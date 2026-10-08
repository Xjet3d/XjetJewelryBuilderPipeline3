# P3 Admin — Dashboard · Sessions · Users · AI Prompts & Params

- **Page:** `{base}/admin/`, i.e. `http://proto/JewelryB2C3/admin/`.
  - The customer site never links to it.
  - It is served with `X-Robots-Tag: noindex, nofollow`.
- **API:** `{base}/api/admin/*`.
- **Code:** `p3/admin.py` (routes and authorization), `p3/usage.py` (activity from application data), and the admin methods of `LocalAccountProvider` (identity side).
- **UI:** `web/admin.html` and `web/admin.js`.

## Authorization

Every admin route calls `RequireAdmin(Ctx, Authorization)`, which returns an `AdminPrincipal`.

- **Today** it accepts the existing developer key: `Authorization: Bearer <P3_ADMIN_KEY>`, the same key that protects `/dev`. With no key configured, admin is disabled (503).
- **The page** asks for the key once and keeps it in `sessionStorage`, so only for that browser tab.
- **Before the public site goes live**, replace the body of `RequireAdmin` with a proper admin role or company sign-in, for example SSO. The routes and UI do not change. Optionally, nginx can also restrict `/JewelryB2C3/admin` to the office network.

## Users — token management (from B2C2 `/admin/tokens`)

| B2C2 | P3 |
|---|---|
| Name, Email optional | **Name and Email required**, email format checked server-side |
| duplicate emails allowed | **one account per email**: 409 with a link to the existing user |
| Max Generations (default 10) | same, 1–9999 |
| 6-letter token, profanity-filtered | same (shared generator) |
| table: Token, Name, Email, Used/Max, Status, Created | same, plus **Last activity**; statuses Active / Unused / Pending verification / Exhausted / Inactive / Removed |
| Edit: name, email, max, reset usage | same (Name/Email still required) |
| Deactivate / Activate | same: deactivating turns off all of the account's tokens, and activating turns its current token back on |
| Remove = hard delete | **soft remove**: tokens revoked and the user hidden (shown again under "Show removed"). Designs and usage history are kept, and the user can be restored. |
| rows not clickable | **row opens the user detail** |

## User detail

Everything is counted from recorded data; nothing is estimated.

- **Account:** created, email verified, first activity, last sign-in, and last activity.
  - Sign-ins are recorded from 1 Oct 2026, in `account_events`: entering a token, or opening a verification/email link.
  - Last activity is the time of the last authenticated request.
- **Generations used (charged):** one per finished 360° movie (B2C2 rule).
- **Job counts:**
  - image generations, refinements (batches and their images), 360° movies, and 3D meshes (developer tool), each with ready and failed counts;
  - total jobs succeeded and failed;
  - designs created (every design is saved automatically) and lines added to the bag;
  - jobs by provider (mock / live) and sign-ins.
- **Usage over time:** daily bars for the last 90 days.
- **Designs gallery:** thumbnails. Clicking one shows every batch, the four options, the selected one, and its movies.
- **Activity timeline:** design, refinement, movie, 3D, bag, sign-in and admin actions.

## Sessions — the main entity

A **session is one design journey**:
- It starts when the customer submits the first prompt of a New Design (`designs.created_at`). A New Design click with no prompt is only counted on the Dashboard ("New Design clicks").
- Reopening the design from My Designs continues the same session.
- It ends when the customer starts another New Design, or after **30 minutes** without activity.

**Stages:** Started → Generated → Customize → Bag → **Checkout Clicked**.
- Checkout isn't implemented. The button stays "Checkout Unavailable" but is clickable, and each click is recorded.
- Refinements are counted alongside the stages, e.g. `Generated → Refined ×2 → Customize → Bag`.
- A session that ended before the Bag gets `→ stopped`.

**Where the data comes from** (code: `p3/sessions.py`):
- **Job tables:** stage times from batches, candidates, customizations, movies and bag lines.
- **`session_events`** (append-only, `pipeline3.db`) adds what those don't keep:
  - option selected, Customize opened, and every material/size/quantity change, each with the **fixed price shown** and its pricing version;
  - bag added/removed (with a price snapshot), Bag viewed, Checkout clicked, design reopened, New Design clicked;
  - admin 3D actions.
- **Backfill:** sessions from before tracking are rebuilt from the job tables, and existing bag lines get a backfilled `bag_added` event.

**Admin views:**
- **Sessions:** image, customer, email, start / last activity, stage path, generations/refinements, size, material, Bag, Checkout Clicked, 3D status, fixed price, with filters.
- **Session detail:**
  - a journey timeline with the time between steps;
  - Generate 3D, plus any results;
  - the Customize choice history and every image of the session;
  - AI requests for the session (cost "—" until configured).
- **Dashboard:**
  - sessions, New Design clicks and the funnel;
  - average refinements and % selected for 3D;
  - 3D averages (volume, weight by material) and the fixed vs 3D price variance once measured.

## Inspiration Gallery

**Publication and privacy (since 2026-10-07).** *Show in gallery* asks whose design it is: an **XJet design** (made by
XJet staff — the default) or a **customer's design**, which is published only with the customer's consent on record
(a note of who agreed, when and how; kept with the item, shown as a badge in the gallery list and on the API as
`owner_kind`, `consent_note`, `consent_at`, `consent_by`). The wording of the consent request to customers is a
pending legal decision. A shared master never exposes its prompt, its reference image or its refinement words to
other customers: they see its images and its name; a refinement forked from it keeps the master's prompt private too.
The share link name is assigned at publication (a share GET never writes).

The gallery on the customer site (home page, first 8, and the Inspiration page) shows **real XJet designs**, chosen in the Admin. Code: `p3/gallery.py`; tables `gallery_items` (the tiles) and `gallery_uses` (customers on them).

- **One shared master design per tile.** A customer who taps **Make it yours** is *linked* to the XJet design (`gallery_uses`); the design, its images, its 360° movies and its Hi3D raw model are never copied. The customer's own selection lives on the link, their Customize choices in `customizations` (one row per customer, design and option), their bag lines in `bag_lines`.
- **Curate:** Sessions → open a session → **Show in gallery** (the design's selected image), or **Use in gallery** under any ready option. ↑ ↓ in the Gallery tab set the display order; **Copy link** gives the customer share link by design name (`…/JewelryB2C3/design/aurora-twist`) — the same link the customer's Share button gives — that opens the design's preview directly (older `#gallery=<id>` links still work); **Remove** takes the tile off the site (customers keep their links; the design stays listed as "removed from gallery" while it has journeys).
- **Customer:** one tap opens a large preview — "Love this design? Make it yours." — with **Make it yours** / **Back to gallery**. Signed in: the Design screen opens on the shared design with the gallery image selected ("Your gallery pick and its three siblings"). Not signed in: the sign-in flow opens and the design opens right after. Starting the same design again moves it to the top of My Designs — never a second entry. The gallery is also reachable from the My Designs sidebar (**Inspiration Gallery**).
- **Sharing:** the lightbox has **Share** on phones (the native share sheet; https only — on plain-http proto phones get Copy link) and **Copy link** on computers. The link is by **design name**, never a Ring ID: `design/<name>` is assigned the first time a design is shared (`designs.share_slug`) and never changes, so links already sent keep working after a rename (the current name resolves too; two designs with one name get `-2`). The page behind it carries the social preview (Open Graph / Twitter): the design name, "Designed with XJet Atelier", the ring image (800 px JPEG) and the canonical link. Messaging apps can only fetch that preview from a public address — proto is internal.
- **♥ Favorites:** a heart on every gallery tile and in the lightbox. A favorite is a saved reference to the master (`gallery_favorites`, per account — it follows the customer across sign-outs and devices); it never creates a My Design or a copy, and Make it yours still links the design to My Designs as usual (a design can be in both). The My Designs panel has two tabs, **My Designs | ♥ Favorites**, and every screen has a way straight to each tab: the header's utility row (My Designs when signed in, the ♥ with the count, the bag, the account), the phone menu, the account menu, and the Design screen's toolbar (**My Designs** and **♥ Favorites** while the panel is closed). On phones and tablets the panel is a drawer over the whole screen, above the header. A design taken out of the gallery disappears from Favorites and comes back with it. Signed out, the heart asks for sign-in and saves the favorite right after.
- **Header:** two rows on the site's pages (from 768 px). The utility row: My Designs, the ♥ Favorites heart with its count, the bag with its count, and the account — a person icon, the name, the coin with the movie generations left, and ˅. The account opens the account menu (name, email, generations, sign-in code, orders, My Designs, Favorites, and **Sign out** at the bottom); Sign out is no longer shown in the header itself. Signed out, the account's place shows Sign in. The main row: XJet Atelier, How It Works · Inspiration · Materials · About (a small menu: About XJet, Technology, FAQ, Support) and Start Designing. Below 1024 px the pages are in the menu; phones have one row (menu, XJet, the icons and the account); the Design screen keeps its own row (XJet Atelier, Design — Customize — Bag, the account, Exit).
- **Metal preview (lightbox):** **Original** (always the default when the preview opens) and Silver / Stainless Steel / Vermeil swatches apply the 360° movie's metal filter to the preview image only — nothing is generated, saved, priced or selected for Customize.
- **Costs:** nothing is generated or charged by Make it yours. A 360° movie that exists is shown at once; a movie a customer asks for on another option is charged to *that customer* (`movies.requested_by`). A **refinement** of a shared design becomes a design of the customer's own (`source_design_id` = the master; event `gallery_refined`), so the master never changes.
- **Remove from My Designs (customer):** a design of their own is hidden; a shared gallery design is unlinked. The Admin keeps the journey ("Removed by customer") and every statistic; starting the same gallery design again restores the link. The bag is not affected.
- **Journeys:** every customer on a shared design is a session of its own (session id `use_…`) — in Sessions, on the user's page and in the Dashboard funnel — with "Started from gallery · R-1013-A". The master design's own page and each customer's page show **Customers on this gallery design** (who, when, path, size, material, bag, checkout). Generate 3D from a customer's journey uses that customer's size and material; the Hi3D model belongs to the design and is shared.
- **Gallery tab:** every master design with usage statistics — Selected (journeys), Customers (unique), Customize, Bag, Checkout, Orders, 3D, Refined, ♥ (customers who saved it to Favorites), last used — sortable as Most chosen / Most popular / Most added to bag / Most ordered / Most saved / Last used; a row opens its customers.
- **Dashboard:** the funnel is also split by origin (own prompt vs gallery). The 3D table by ring size states how many 3D parts (results) and Hi3D models it rests on: created, measured, and used in the averages (models without a round bore or with an open mesh are left out).

## Generate 3D (admin only)

`POST /api/admin/sessions/{id}/3d`, code in `p3/production3d.py` and `p3/geometry.py`.

- **Never automatic.** Only this admin action starts Hi3D v3.0, on the session's selected option.
- **Ring size:** the customer's size, else **US 10**; the admin can override it before generating. Stored as `customer_size` and `production_size`, with `size_source` = customer / default / admin_override. Material is handled the same way: customer, default, or admin override.
- **No repeated paid calls.** If a raw Hi3D model already exists for that option, it is reused. Retrying a failed local step never repeats Hi3D while the raw STL exists ("Retry geometry (no Hi3D charge)"). Only a failed Hi3D request offers "Retry Hi3D (paid)".
- **One Hi3D model per design (safeguard).** Once a design has a valid Hi3D model, every later request — any size, material, selected option or customer journey — reuses it: the button reads **Recalculate geometry**, the page says *Existing 3D model available* (… *from Gallery Master* on a customer journey), and no Hi3D call is made. A second paid model (for a genuinely different option) is only possible through **Generate a completely new Hi3D model instead…**, which warns and requires typing `GENERATE NEW 3D`; the server refuses the request without that exact phrase (`hi3d_model_exists`) and records the override on the journey and in the Dashboard's **Admin activity** log. A refined design is its own design and gets its first model normally.
- **Hi3D settings:** 2048quality, 5,000,000 faces, STL only.
- **Download:** the STL is streamed straight to disk (`meshes/<id>/original.stl`), with its SHA-256 calculated during the download. Hi3D's thumbnail is saved too.
- **Measure once** (`ring-measure-once-v3`, `p3/geometry.py` `MeasureRaw`). The full raw STL is measured exactly once, and the results are stored in `raw_geometry`:
  - raw X/Y/Z and inner diameter;
  - volume and surface area;
  - ring frame (orientation) and bore centre;
  - SHA-256 and method version.

  How it works:
  1. Find the ring axis from the exact surface moments.
  2. Find the bore: exact cross-sections at 9 heights across the band, combined — the narrowest wall over the heights,
     what a finger must pass. The **inner diameter is the largest circle that passes** (what a ring gauge reads), so a
     US 10 ring passes a Ø 19.76 mm gauge exactly; the widest diameter of the hole is shown next to it, and a bore more
     than 4% off round is flagged for review (the ring is loose its wide way — *Make the bore round* scales it round).
  3. Compute the volume from two reference points. If they agree, the mesh is probably closed. This is a cheap heuristic, not proof of watertightness.
- **Any size or material is arithmetic** (`Scaled`), with no file read: s = target ID / raw ID, then lengths × s, area × s², volume × s³, and weight = volume × density (`config/materials.json`). US size → mm: 11.63 + 0.8128 × size.
- **Status:** `measured`, or `needs_review` if:
  - no bore was found;
  - roundness deviation is over 4%;
  - the closed-mesh heuristic disagrees.
- **Production state is separate from processing** (`production_state`): `processing` · `complete` · `review_required` · `failed` · `cancelled`. A result is never shown as "Ready" while it carries a warning: it reads **Processing complete — production review required**, and the reasons with the recommended next step (`review`: no bore → inspect / model another option; bore not round → check the inner diameter; open-mesh heuristic or open edges found by the background edge check → repair before production) are shown *before* the measurements. Viewing the model or downloading the STL never approves it. The Sessions list shows the same state in its 3D column (`three_d_state`).
- **Existing model (one per design):** the panel names the model — *Uses Master model R-1013-A* — and offers the normal actions that reuse it: **View 3D**, **Recalculate geometry · US n · material**, **Prepare / download STL** (enabled once a result exists for the journey's size and material). None of them calls Hi3D.
- **File names** are operational: `<Design-Name>_<Ring ID>[_<Order ID>]_<Material>_US<size>.stl` (e.g. `Aurora-Twist_R-1013-A_ORD-10482_Silver_US10.stl`); the raw Hi3D model is `<Design-Name>_<Ring ID>_raw.stl`. The Order ID appears only when an order exists.
- **Background, never blocking the numbers:**
  - a light preview (~25k faces, `preview.p3pv`; visual only, never used for numbers);
  - a mesh-integrity check (edge manifoldness → `raw_geometry.integrity`).
- **Scaled STL:** never stored. "Download scaled STL" queues an export job that writes a temporary STL (`exports/`), aligned with the bore centre at the origin, axis Z, in millimetres. That frees the processing slot; the browser then downloads it natively through a signed link. The file is deleted after 1 hour. Requests measured before v3 keep their stored scaled STL.
- **Accuracy:** tested against an ideal ring in a random orientation and arbitrary units. On a 5M-face torus, inner diameter and volume match the analytic values.
- **Stored per stage** in `geometry_results` (raw and production), with weight and price in `price_calculations`.

### Charms: a 3D path of their own

The same button, safeguards and queue apply, but a charm design takes a separate path (`p3/charmgeometry.py`). The
ring path above is unchanged.

- **Hi3D settings:** the charm's own `hi3d-charm` configuration. Version 1 is the same 2048 quality, 5,000,000 faces
  and STL as rings.
- **Size:** a height in mm. Defaults:
  - the customer's charm size;
  - otherwise the middle size on offer (`products.CharmDefaultSize`: 20 mm of 15–30).

  The Admin may choose any size between 3 and 100 mm, for example a size no longer offered, for an existing order.
  Only charm materials are offered, with their customer names.
- **Measure once** (`charm-measure-once-v1`, `MeasureCharmRaw`). There is no bore and no ring frame:
  - **thickness** = the direction of least spread;
  - **height** = the model's up direction (Hi3D models are Z-up) laid into the charm's face plane;
  - **width** = across.

  A model lying flat falls back to its longest direction. Volume, area and the closed-mesh heuristic are as for rings.
- **Scaling:** s = target height / measured height, then lengths × s, area × s², volume × s³, all arithmetic.
  - A charm's size is its **total height, the loop included** (`products.CharmSizeDefinition`), so the whole charm —
    body and loop — is scaled to it (`products.Charm3DHeight`). There is no loop detection or measurement.
  - A charm result is `measured` (complete) like a ring; only a real problem, such as the closed-mesh heuristic
    disagreeing, makes it `needs_review`.
  - There is no wall-thickness check or printability rule.
- **Weight, cost and 3D price:**
  - Weight = volume × the material's density.
  - Production cost and 3D price come from the charm price book only (*Pricing & Materials → Charm*).
  - A result waiting for charm $/g is priced as soon as the charm table gets them; a ring price change never prices
    a charm.
  - The customer price shown beside it is the charm price for the produced material and size.
- **Scaled STL:** exported on demand, lying flat (width X, height Y, thickness Z), centred, in millimetres, with the
  header `XJet P3 scaled charm 20 mm total height incl. loop`. Its file name gives the size in mm:
  `Lune-Drop_C-1003-B_ORD-10482_Sterling-Silver_20mm.stl`.
- **The panel** shows *Height* (target and result, "total height incl. loop") in place of *Inner diameter*, sizes in
  mm, and the charm explanation under *What was done to the model*.

## Working in the Admin

- **Sign-in is remembered per browser** (server-side session cookie) until *Sign out*; *everywhere* revokes every browser. The key itself is never stored in the browser.
  - The cookie is the Atelier's own (`atelier_admin_session`), sent only to this app's API path (`/<base>/api/`), `HttpOnly`, `SameSite=Strict`, `Secure` over https. Exactly two things sign an admin in: that cookie, or `Authorization: Bearer <P3_ADMIN_KEY>` (scripts). Anything else — a Basic/Digest header a browser replays after signing in to another app on the same host, another app's cookies — is ignored, so no other application on the host can collide with the Admin sign-in (2026-10-07). Browsers signed in before that date sign in once more.
- **Navigation:** Dashboard · Sessions · Users · Orders · Gallery · Settings (Pricing & Materials · Promo codes · AI models & prompts · System health). Old `#/models/…` links redirect.
- **Dashboard** in operational order: Needs attention → KPIs → funnel (own prompt vs gallery) → gallery top designs → orders → 3D geometry and price accuracy → admin activity, with a time range (Today · 7 days · 30 days · All time; 3D model statistics stay all-time). One vocabulary everywhere: *Journey* = one customer on one design · *Selected* = an option chosen (a gallery pick counts from the start) · *Customize* · *Bag* · *Checkout* = opened the checkout · *Order* = placed an order · *3D* = admin 3D requests · *Refined* = refinements / forks. Copies made before shared master designs are labelled "Gallery copy · legacy" in Sessions and are not counted as gallery journeys.
- **Needs attention** (`GET /api/admin/attention`, also on the Dashboard and as a Sessions filter with a count): 3D results that need production review or failed, failed design generations, new orders to review, failed payments, payments still pending after 3 days, failed address validation, paid orders without a 3D model, open quote requests.
- **Sessions list:** search by Ring ID (`R-1013`), option ID (`R-1013-B`), design name, Order ID, customer or email; filters for stage, bag, 3D, mock; sort by newest, last activity, customer or price; a miss says *No matches for "R-1015"* with *Clear search* / *Reset filters*. Filters, sort and scroll position are kept when you open a session and come back; the session page has *Prev / Next*. Phones get one card per session; wide screens a two-line row (customer · ring · stage · choice · price · status) with a sticky header.
- **Session page:** a sticky header (customer, Ring ID, stage, 3D state, *Generate / Recalculate 3D*, *Download STL*, *Show in gallery*, Prev / Next) and **Rename** (gallery master designs must have distinctive names — a name another master already uses is refused unless confirmed; the Gallery tab flags duplicates). Secondary sections (AI pipeline, designs in this session, journey) are collapsed by default. Prices are never two numbers called just "Price": *Customer price* (with its source and pricing version), *Price shown to the customer then*, *Customer price at this request* on 3D results, *Production cost*, *3D estimate*.
- **Journey (expanded):** the customer's own words (the prompt, each refinement) read as input — muted grey italic — and the image they gave for that step sits beside them: their uploaded or pasted reference for a new design, the option they refined for a refinement. Hover enlarges it (desktop), a click opens it full size with *Download original* (the image as received, re-encoded as PNG without metadata), and a small download icon sits next to the thumbnail. Steps without an image show no placeholder; the collapsed Journey is unchanged.
- **Dialogs:** confirmations, paid actions and share links use in-app dialogs and toasts; nothing uses the browser's alert / confirm / prompt. Rows are keyboard-operable and focus is always visible. Every image enlarges on hover.
- **Names:** every design is named locally from its own prompt by deterministic word rules (`p3/naming.py`) — no AI, vision or provider call, zero cost. One or two words, no "The", no "Ring", no material, karat, size or brand: a *family* word from the motif or style (Serpent, Luna, Amour, Rose, Fil, Bold, Halo …, or a proper noun the prompt is built around) plus a *descriptor* from the prompt's keywords (twist, lattice, weave, wave, bloom, heart, facet, open, curve, texture …), e.g. "Aurora Twist", "Fil Wave", "Halo Bloom", "Amour Curve". Names are unique across all designs: a taken name gets another word combination ("Fil Twist" → "Fil Spiral" → "Fil Helix"), and a number ("II") only when every word is used up. A customer's refinement of a gallery master is a design of its own that keeps the lineage — the master's family word plus a descriptor from the refinement instruction ("Fil Twist" + "make it a lattice" → "Fil Lattice"). Renaming a master renames the variations that share its family word ("Fil Lattice" → "Aurora Lattice" under a master renamed "Aurora Twist"). **Rename** is the final override: it offers local suggestions (`GET /api/admin/designs/{id}/names`) and accepts any name; a name another master already uses is refused unless confirmed. On a variation whose master already has a 3D model, *Generate 3D* is not open: the panel points to the master's model (reuse it from the master's session) and a new paid model needs the typed confirmation (`hi3d_source_model_exists`).
- **Legacy copies (one ring twice):** before shared master designs existed, *Make it yours* copied the XJet design — a second design with its own Ring ID and identical images ("Fil Twist R-1012" and "Fil Line R-1015" were the same ring). Such copies are flagged *Gallery copy · legacy* in Sessions and listed under Needs attention; the session page offers **Merge into master**. The merge folds the copy back (`POST /api/admin/designs/{id}/merge`, `p3/merge.py`): the customer's journey becomes their link to the master (or part of XJet's own session when XJet made the copy), every Customize choice, bag line, order line, quote request, 3D request and model, movie, event and fork now points at the master's matching option, so the master's 3D model and STL serve the copy's orders (an order gets a note), and the copy's Ring ID is retired for good — never reused; a search for it says where it went. Only a true copy is merged (no refinement of its own, images identical to the master's).
- **A master is never changed by a refinement.** Once a design has a 360° movie (made or being made), a 3D request, an order or quote request, or a gallery tile, a refinement of it — by a customer *or by its owner* — is saved as a new design named in its lineage (event *Refined into a new design*, with the reason); the customer app says so under the composer and after the refinement. A design that is still images only may be refined in place. For refinements that landed inside a master before this rule, the session page's *Designs in this session* offers **Make it its own design** (`POST /api/admin/batches/{id}/split`): the refinement and everything made from its images (movie, Customize choices, bag lines, orders, quote requests, 3D requests, recorded steps) move to a new design, the master's selection goes back to the option that was refined (or its gallery image), and the master is otherwise untouched. A refinement that is the design's gallery image cannot be moved.

## Checkout & Orders

The customer path for fixed-price materials (Stainless Steel, Silver, Vermeil): **Bag → Checkout → Your details → Shipping address → Review & pay (promo code, payment, terms) → Order confirmation**. Gold is not on this path: Customize shows **Request a quote** instead, which records a `quote_requests` row (Q-5001 …), emails the customer and appears in Admin → Orders. The Legacy JewelryB2C flow was the functional reference (step structure, shipping options, policy wording, prefill rules); its browser-only "order", random numbers, mandatory coupon and client-side prices were not carried over.

- **Server-authoritative.** Every unit price is the material's *current* fixed price at the moment of ordering (the bag flags a line as "repriced" when its snapshot differs); promo discounts, shipping and totals are computed on the server; the browser only mirrors the rules for instant feedback. An empty or non-orderable bag cannot be ordered; one click = one order (`client_request_id`).
- **Order = snapshot.** `orders` (ORD-10001 … assigned by a trigger, never random, never reused) + `order_lines` (design name, Ring ID, material, size, quantity, unit price, line total, pricing version, image) + `order_events` (placed, email, status, payment, note — with who did it). Nothing is recomputed later.
- **Customer details:** first name, last name, email, phone (validated). The known account name/email pre-fill the form and never overwrite what the customer typed.
- **Shipping address** (`p3/addressing.py`): recipient, street, line 2, city, state/province (required for US, CA, AU, BR, MX, IN, CN), postal code (country patterns; none for AE, HK, QA, BH), ISO country. `AddressValidator` is the seam for an external service: the default `NoValidator` stores the address as `unverified`; a provider returns `verified`, `corrected` (with a suggestion the customer confirms — nothing is replaced silently) or `failed`. The verdict travels with the order (`address_validation`).
- **Promo codes** (`p3/promos.py`, Settings → Promo codes): percent or fixed, active flag, validity dates, usage limit + count, optional material restriction, minimum subtotal. Validated server-side only; the order keeps `promo_json` (code, kind, value, original amount, discount, final amount). Usage is counted inside the order transaction, so a limit is never exceeded.
- **Payment** (`p3/payments.py`): a `PaymentProvider` adapter (Begin/Confirm). No provider is connected (`P3_PAYMENT_PROVIDER=none`): orders are created with `payment_status = pending` and say so plainly — a successful payment is never faked. An admin can **Record a payment** by hand (bank transfer, phone) with a mandatory note; it is logged as manual with the admin's name. Statuses: pending · paid · failed · refunded · cancelled. Do not reuse HTTP 402 for payment errors (it means "generation quota exhausted").
- **Terms** are accepted explicitly; the order stores `terms_version` and `terms_accepted_at`.
- **Confirmation**: screen + email (outbox in mock mode, SMTP in live) with Order ID, every ring (image, name, Ring ID, material, size, quantity, amount), totals, address, payment status and "what happens next". No production cost or 3D price ever reaches the customer.
- **Lifecycle** (Admin → Orders): `new → payment_confirmed → three_d_ready → production → qc → shipped → completed`, plus `cancelled`; completed is final. Recording a paid payment on a new order moves it to payment confirmed. Every change is in the order's history with a note.
- **Admin → Orders** lists Order ID, date, customer, email, ring/design + Ring ID, material, size, quantity, promo, total, payment, address validation, 3D state and order status; search by Order ID, Ring ID, design name, customer name or email; filter by status and payment. The detail page has the status / payment / note actions, the rings to make with their 3D state and **Download STL** (file name with the Order ID), the address, totals, terms and the history.
- **Sessions & statistics:** the funnel is Started → Generated → Selected → Customize → Bag → Checkout → Order; a journey shows its order references; the gallery statistics count customers who ordered a master design; the dashboard has an Orders block (open, payment pending, paid revenue, reserved value, open quote requests).

### Processing queue and live status

- **One job at a time.** Heavy local work on the 5M STL runs one job at a time in a persistent queue (`geometry_jobs`, `p3/geoqueue.py`). Each job runs in a memory-capped worker process (`p3/geometry_worker.py`, `P3_GEOMETRY_MEMORY_MB`). Priority order: export, then measure, then preview, then integrity.
- **Restarts:** after a restart, a running job is queued again; it fails if it is interrupted twice.
- **Cancel:** a queued job can be cancelled before it starts.
- **Real stages, with persisted start and end times** (`stage_log`), so a refresh shows the same state:
  - `Waiting for Hi3D — 00:42`
  - `Generating 3D — 03:11`
  - `Downloading STL — 64% — 00:08` (a real byte count)
  - `Queued — 1 ahead`
  - `Calculating Geometry — 00:06`
  - `Ready — Total 04:37`

  No other percentages are shown. The page polls only `GET /api/admin/3d/{id}/status`, every 2 s; the clock ticks in the browser, synced to server time.
- **Endpoints:**
  - `POST …/3d/{id}/cancel`, `…/retry`, `…/export`;
  - `GET …/3d/{id}/export/{job}`;
  - `GET /api/admin/storage` (3D bytes and free disk; shown on the Dashboard, with no automatic deletion).

## Quote requests (Orders → a quote request)

**Code:** `p3/quotes.py`, `p3/orders.py` (`CreateFromQuote`). **API:** `GET /api/admin/quote-requests/{id}`,
`POST …/offer`, `POST …/note`, `POST …/status`; the customer's page `GET /quote?token=…`, `POST /quote/approve`,
`POST /quote/reject`.

- **Every detail.** The customer (name, email, phone), the design (image, Ring/Charm ID, session link), material, size,
  quantity and message; where the request notification went (`P3_STAFF_NOTIFY_EMAILS`, or none configured), whether
  the customer's confirmation was sent, and the address the quote goes to (the customer's email). The customer's
  request dialog says the same address.
- **Price suggestion.** When the design has a measured 3D model, its weight at the requested size in the requested
  material (volume × density) × that material's price per g (Settings → Pricing & Materials; charms: the charm
  table). A model of another option is used as an estimate and says so. Size, weight, price per g, price per piece,
  quantity and validity are all editable; weight × price per g fills the price until the Admin types one.
- **The reply.** A suggested message (no numbers in it: the email's quote box carries them) to edit, and an internal
  note for the history. Sending emails the quote with **Approve the quote** and **Decline the quote** on the
  customer's own link (stored hashed). A revised quote replaces the link: the earlier email's buttons then say a newer
  quote exists. The link can be copied from the workspace (useful while mail delivery is not active).
- **Approve** asks for the shipping address, the delivery option and the terms, then turns the quote into an order at
  the quoted price: one line, `pricing_version` `quote:Q-5001:v1`, the order notes say which quote it came from; one
  order per quote; payment as for every order (pending until arranged). **Decline** marks the quote rejected; the team
  is told when staff recipients are configured. An expired quote can no longer be approved.
- **History.** The request, the emails, notes, each quote sent (with its numbers), status changes, the customer's
  decision and note. An approved quote's status is changed through its order.

## Fixed price vs production cost vs 3D price

Three separate values that never overwrite each other:

1. **Fixed customer price:** the price the customer saw. It is the Add to Bag snapshot, else the price shown in Customize, else the current list price, always with its pricing version. Customer pages never read 3D data, so this price can't change.
2. **Production cost:** weight × cost $/g.
3. **3D calculated price:** weight × price $/g.

Weight = 3D volume × sintered density.

All four inputs come from one versioned table, **Admin → AI Prompts & Params → Pricing · materials** (`p3/materialprices.py`, table `material_price_lists`): density, price $/g, cost $/g and fixed price, for each material.

- **Fixed price** is what the website shows for the material. Empty means "NA": the website shows "Price unavailable" (luxury gold today). It is a placeholder until there is enough data to set it from real 3D costs.
- **Density** in this table is the only density used: weight on the website and in 3D.
- **Saving** creates a new version (who, when, note). New 3D results and website quotes use it immediately. Results already calculated keep the version they were made with.
- **Seed:** the business table of 2026-10-02 (Silver 925 / 316L / Vermeil / Gold 18K / 14K). 10K gold had no values.
- An empty price or cost shows "—", with the reason. Nothing is invented.

## Cost reporting (prepared, not active)

Each usage event in `accounts.db` now records:

- `provider` (mock | fal), `endpoint` (model id) and `mode` (mock | live);
- `cost_usd` and `cost_source`, left NULL for now.

Events recorded before this change were filled in from the job tables at startup (`p3.usage.BackfillUsageAnnotations`).

The detail view's **Provider usage** table already groups requests by kind, provider and endpoint. To report cost per user:

1. add a provider price table (endpoint → cost per unit, versioned);
2. set `cost_usd` and `cost_source` when usage is recorded, or compute them in the report.

Until then the Cost column shows "—".

## Tests

`tests/test_admin.py` covers:

- protection (customer token rejected, admin disabled without a key);
- Name/Email validation and duplicate emails;
- edit / deactivate / activate / soft remove / restore;
- detail counts after a design, a refinement, a successful movie and a failed one;
- the provider ledger, sign-in recording and the usage backfill.

## AI Prompts & Params

**Code:** `p3/modelconfig.py`. **UI:** the *AI Prompts & Params* tab. **API:** `/api/admin/models/*`, which needs the admin code.

**Models.** All five endpoint identifiers were checked against the existing integration and fal.ai's OpenAPI on 1 Oct 2026.

| Tab | Endpoint | Used for |
|---|---|---|
| any-llm | `fal-ai/any-llm` | **Not used by the P3 pipeline yet** (B2C2 uses it for its prompt gate). Settings can be prepared and previewed. |
| nano-banana-pro | `fal-ai/nano-banana-pro` | New Design without a reference image: 4 separate requests, 1 image each |
| nano-banana-pro/edit | `fal-ai/nano-banana-pro/edit` | Refinements (selected image), and New Design with an uploaded reference |
| minimax camera | `minimax/h3-max/camera-controls` | The 360° movie (selected final image) |
| hi3d | `hitem3d/hi3d/v3.0/image-to-3d` | Admin Generate 3D and the developer mesh tool |

**Ring | Charm — the configuration being edited.** Above the model tabs, *Product configuration: Ring | Charm* chooses
which product's configuration the page shows; only that product's models are listed. Each product has its own models,
versions and history:

| Product | Models |
|---|---|
| Ring | `any-llm`, `nano-banana-pro`, `nano-banana-pro-edit`, `minimax-camera`, `hi3d` (the table above, unchanged) |
| Charm | `any-llm-charm`, `nano-banana-pro-charm`, `nano-banana-pro-edit-charm`, `minimax-camera-charm`, `hi3d-charm`: the same endpoints, their own versions |

Every Ring model has its Charm counterpart. **any-llm · Charm** is the Charm request check — like the Ring one, it is
not used by the P3 pipeline yet. It has its own model, instructions, parameters, versions and history. Version 1
copied the Ring Any-LLM's active settings by value (model, temperature, token limit, priority, reasoning) with its
own charm instructions (`config/prompts/charm_anyllm_system.txt`, the same JSON answer as the Ring check).

- Saving, activating or restoring a Charm version never changes a Ring prompt, parameter, version or active pointer, and
  the reverse is also true. A version can only be restored into its own model (tested).
- The pipeline picks the configuration from the design's product. A charm design, its refinements, its movie and its 3D
  use the Charm models, and the batch, movie or mesh records the Charm version id.
- **Charm version 1** copies the provider settings of the Ring model's active version at the moment the Charm models were
  first created, with the Charm's own prompts (`config/prompts/charm_*.txt`, listed in `config/generation.json` →
  `charm`) and its own four refinement directives. From then on the two are independent. `hi3d-charm` starts with the
  Ring model's active 3D settings, unchanged.
- *Preview request* for a Charm model uses a charm sample text. *Export all {Ring | Charm} models* exports only the
  product being edited; the API is `GET /api/admin/models/export?product=charm`, and rings remain the default.
- `/api/admin/health` (admin key) lists `config_versions` (Ring, unchanged) and `charm_config_versions`; the public `/api/health` says only that the service is up (plus the AI mode outside production).
- This selector is not the customer switch. Whether customers can see charms is set in **Settings → Products**.
- Known inconsistencies in the Ring prompts are documented, not fixed, in `docs/RING-PROMPT-NOTES-2026-10-05.md`.

**Source of truth.**
- Every new request is built from the model's **active version**. The version id is recorded on the batch, movie or mesh (`config_version`).
- A request created before an activation keeps its version, even if it is submitted afterwards.
- Version 1 was seeded from `config/generation.json` and `config/prompts/*`, and sends exactly the same requests as before (tested). Those files are no longer read for model parameters.
- The old hardcoded "text + suffix" is now the editable image prompt template `{{user_text}}

<suffix>`. No suffix is added on top of it.

**Refinement variations (nano-banana-pro/edit).** A refinement sends four requests with the same instruction and the same reference image; the edit model honours seeds only weakly and is told to preserve everything else, so the four results often came back as the same picture. The edit model's configuration therefore has four pipeline-only fields, `variation_a` … `variation_d`, one directive per image: A the most faithful / conservative version, B a bolder version of the requested change, C an alternative interpretation in proportions or placement, D a different finish or detail treatment — all still preserving the rest of the design. The directive is appended to the end of that image's prompt (the batch's shared prompt stays as rendered); a blank field means that image gets the plain prompt, exactly as before. Refinements only — New Designs from an uploaded photo are untouched. No extra model, request or cost. Existing installations received the four default directives as a new visible version on the first start after the update; edit or blank them in Admin → AI Prompts & Params → nano-banana-pro/edit, and *Preview request* shows image A's payload with all four directives listed. The defaults are deliberately far apart — A faithful, B the change pushed to an extreme, C the proportions and placement rebuilt (the silhouette may change), D a free variant that may be a completely different ring in the same family; B–D state that they override the "preserve everything" instruction where the two conflict. When a stronger default set ships, an installation still running the earlier defaults unedited is moved to the new ones as a new visible version; edited texts are never touched.

**Configured vs omitted.** Each parameter is either *configured* (sent) or *not sent*, in which case the provider default applies and is shown in the form. Required provider parameters are always sent: the image `prompt`, and the movie `prompt_expansion_mode`.

**Runtime inputs.** These are supplied by the pipeline and never stored in a configuration:
- **Image models:**
  - `{{user_text}}`: the customer's prompt or refinement instruction. Required in the image prompt templates.
  - `{{design_prompt}}`: the session's original prompt. Optional.
  - `image_urls` (edit model): the selected or reference image.
  - `seed`: random, different for each of the 4 images.
  - `num_images`: always 1.
- **Movie:** `image_url` is the selected final image.
- **Hi3D:** `image_url` is the selected design image.
- **any-llm:** `{{user_prompt}}`.
- **Not sent:** `sync_mode`. The pipeline downloads results from their URL, so the provider default (false) applies.

Saving can't replace these with example text or a fixed URL: such fields are refused, and templates must contain their placeholder.

**Validation** runs before activation, against each model's supported parameters:
- types, choices and ranges;
- unknown or pipeline-controlled fields;
- unknown or missing placeholders;
- camera keyframes: 2–12 keyframes in time order, elevation −90…90, distance > 0, at most 32 turns of azimuth travel;
- Hi3D `export_format`: limited to GLB/OBJ/STL, because the geometry measurement can only read those.

**Saving and history.**
- **Preview** shows the exact request payload with clearly labelled `[SAMPLE …]` runtime inputs, and never calls the provider.
- **Save & Activate** creates an immutable new version. If nothing changed, no version is created.
- **History** lists every version with its date, author and note. *Load into form* lets you review or edit an old version; *Restore & activate* makes a copy of it the new active version.
- **Movie reuse:** an existing movie is reused when its version has the same parameters as the active one. That includes movies made before versioning, through version 1's legacy alias. Changing movie settings means the next new movie request uses them. Customers who reopen Customize on an option get a new movie (charged as usual) only when its settings actually changed.

**Export:** TXT (readable) or JSON (structured: model ids, endpoints, version ids and numbers, parameters, which parameters are omitted, and the pipeline-controlled fields), for one model or all. Configurations contain no API keys, and the export includes none.

## AI mode (Settings → System)

The **Mock mode** switch (ON = mock: no provider is called, nothing is billed). Turning it OFF switches to live — every
generation, movie and 3D request becomes a paid fal.ai call — and asks for confirmation; it needs a fal.ai key (set one
below) and is refused while generations are running. The choice survives restarts (`<data dir>/runtime.json`), exactly
like the `/dev` page's switch. Where the configuration fixes the mode (`P3_LOCK_MODE`, always in production) the switch is
disabled. API: `GET|PUT /api/admin/ai-mode` (`PUT {"mode": "mock"|"live", "confirmation"}`).

## fal.ai API key (Settings → System)

The card shows whether a key is configured, where it comes from (`set here` or `from the server configuration`) and
its last four characters — the key is never returned by any API. **Check and save** first makes a free test upload to
fal.ai (no model runs, no cost); a key fal.ai rejects is not saved. A saved key lives in `<data dir>/fal_key.secret`
(mode 0600), replaces `FAL_KEY` from the environment, survives restarts, and is rebuilt into a live provider at once
(refused while generations are running). **Check key now** tests the key in use the same free way (also in production; changes nothing). **Remove saved key** returns to `FAL_KEY` (refused in live mode when there is
none: switch to mock first). The AI mode is not changed by saving a key. **In production** the key belongs to the server
configuration (`/etc/xjet-atelier/env`): the card is read-only and a saved file is ignored. API:
`GET|PUT|DELETE /api/admin/fal-key` (`PUT {"key"}`), `POST /api/admin/fal-key/check`.

## Products — rings and charms (Settings → Products)

**Code:** `p3/products.py`. **API:** `GET /api/admin/products` and `PUT /api/admin/products/availability`.

- **One switch per product, independent of the others:** *Rings available to customers* (ON by default) and *Charms
  available to customers* (OFF by default). A future product joins the same list (`products.All`; its availability
  key is `<plural>_available`). The API takes `{"product": "ring" | "charm", "available": true | false}`; the older
  `{"charms_available": …}` body still works. The log records `rings_available` / `charms_available` changes.
- **A product that is OFF** is not on the Design screen, not in the Inspiration Gallery or its filters, not in the
  sitemap, and the site's wording (hero, FAQ, Materials, Terms, checkout) does not mention it; a request to start a
  design of it is refused as if the product did not exist. Switching every product off leaves customers nothing to
  design — the site says so. The meta description names the products on offer.
- **360° movie per product: ON / OFF** (default ON for every product; `PUT /api/admin/products/movie
  {"product", "on"}`; log keys `rings_movie` / `charms_movie`). OFF: Customize makes no movie and shows the still image
  only; an explicit movie request answers 409 `movie_off`; the movies that already exist (a gallery master's included)
  stay hidden until the switch is ON again, when they are shown and reused without a new credit. The homepage hero
  plays only the story of an XJet design of a product on offer whose movie is ON; otherwise it shows a gallery
  design's still image.
- **Charms available to customers: ON / OFF.** It is OFF by default.
  - **OFF:** customers see the ring-only site of before: no product choice, no charm text, no charm tiles, favorites or
    share pages, and the catalog and gallery answers are unchanged. A request to create a charm is refused as if the
    product did not exist.
  - **Admin preview:** a browser signed in to the Admin sees and can test charms on the customer site while they are
    OFF.
  - **A simple switch.** Turning it on asks *Enable Charms for customers?* (Enable Charms | Cancel); turning it off asks
    *Hide Charms from customers?* (Hide Charms | Cancel). No phrase is typed. Charm designs, orders and settings are
    kept when charms are hidden.
  - **What customers see while ON:**
    - "What would you like to design? Ring / Charm" on the Design screen (Ring is the default);
    - charm sizes with their prices in Customize;
    - "Charm ID … · 20 mm" lines in the bag, checkout, confirmation and emails;
    - *All · Rings · Charms* in the Inspiration Gallery;
    - FAQ and Terms that name rings and charms.

    While OFF, all of this is absent, and the customer API answers carry no product fields. A customer who already
    holds charms still sees their own charm orders correctly. The Admin preview shows the ON experience marked "Admin
    preview · charms are hidden from customers".
  - Every change is logged with its date and the Admin who made it.
- **Charm sizes** are the charm's **total height, including the attachment loop** at the top: a 14 mm charm is 14 mm
  from its lowest point to the top of its loop. This applies everywhere: Customize, the bag, checkout, orders, emails,
  prices (per size), the 3D scaling and the STL. The definition is kept in one place (`products.CharmSizeDefinition`
  and `products.Charm3DHeight`), so it can be changed later. The sizes on offer (2026-10-06): **10 mm — Delicate**,
  **14 mm — Classic · Recommended** and **18 mm — Bold**; the earlier 15 / 20 / 25 / 30 mm are no longer offered.
  - Edit them in *Charm sizes* (`PUT /api/admin/products/charm-sizes`): sizes between 3 and 100 mm, at most 12, each
    with an optional name, and one of them recommended — Customize suggests it ("Recommended: 14 mm — Classic — tap it
    to confirm, or choose another size") and a 3D is made in it when the customer chose no size. Every change is
    logged.
  - Ring sizes are not affected.
  - Each size has its own price (see *Charm pricing* below). A size without a price shows "Price unavailable".
  - Removing a size that a customer's bag still holds is allowed. The Admin is told how many bag lines are affected,
    and those lines ask the customer for another size before checkout.

### Credits (Settings → Products → Credits)

**Code:** `p3/credits.py`. **API:** `GET /api/admin/credits`, `PUT /api/admin/credits/tariff`.

- **The tariff** — credits per design request, refinement request, additional option, 360° movie and 3D model
  (defaults 1 / 1 / 1 / 1 / 0; a credit is a customer action, never an output file) — is edited here and logged with
  the other product settings. A credit is reserved when the action starts and charged once it delivers; an action
  whose every image fails is released; selecting, materials, sizes and reused movies are free; the Admin's own movies
  and 3D models are never charged. The Users page shows each customer's used / reserved / remaining credits
  ("Credits allowance" when creating or editing).
- **AI spend today** — the estimated list-price cost of the day's live submissions (every submission is recorded with
  its estimate) against the daily cap `P3_DAILY_AI_SPEND_CAP_USD`; at the cap, paid submissions stop with
  `spend_cap_reached` (the customer reads "AI generation is paused for today") until the next UTC day.
- **Request limits** (`p3/ratelimit.py`, `P3_RATE_LIMITS`): paid starts per account and per IP, sign-in attempts per
  IP, registrations per IP and per email; a refused request answers 429 with a Retry-After.

## Charm pricing (Settings → Pricing & Materials → Charm)

**Code:** `p3/charmprices.py`, table `charm_price_lists`. **API:** `GET` / `PUT /api/admin/charm-prices`.

*Material pricing for: Ring | Charm* chooses which product's table is shown. The Ring view is the table described
above, unchanged. The Charm table is completely separate:

| Column | Meaning |
|---|---|
| One column per charm size, e.g. *20 mm $* | The fixed price the website shows for a charm in that material and size. Empty = NA ("Price unavailable"). |
| Price $/g | Charm 3D calculated price = weight × price $/g |
| Cost $/g | Charm production cost = weight × cost $/g |

- **Materials:**
  - Stainless Steel, Sterling Silver and 14K Gold Vermeil are sold at a fixed price per size.
  - Gold is quoted individually ("Quote", the Request a quote flow), like a gold ring.
  - These names are how a charm's material reads to the customer; ring names are unchanged.
- **Never a ring price.**
  - A charm without a price for its material and size is "Price unavailable" and cannot be added to the bag.
  - The ring's fixed price, price $/g and cost $/g are never used for a charm, and the reverse is also true (tested).
- **Weight** uses the material's sintered density from the Ring view (a property of the material). Every price and
  cost in the Charm table is the charm's own.
- **Versions.** Each save is a version `charms-v<N>` with who, when and a note.
  - A charm in a customer's bag shows "repriced" when the charm table changes; checkout uses today's price.
  - A ring line is never repriced by a charm change, and the reverse is also true.
- **Seed:** the table starts empty. No charm price was invented.

## AI cost estimates (Sessions)

**Code:** `p3/aipricing.py`. **Admin:** *AI Prompts & Params → AI prices*.

- **Where costs come from.** A versioned price list per endpoint, seeded from the fal.ai pricing API (`GET https://api.fal.ai/v1/models/pricing`, read-only) and the model pages (1 Oct 2026). *Refresh from fal.ai* updates the unit prices with the server's key; the key is never shown. *Edit* changes the list by hand, and every change is kept as a new version.
- **How a P3 request is priced:**
  - images: per image, $0.15 (one 1K image per request);
  - movie: per second by resolution × the movie's duration (taken from the request's recorded configuration version);
  - Hi3D: credits × $0.02, where credits = geometry (90 at 2048quality, 440 at 2048master) + texture 10 and PBR 5 when enabled.
- **What is counted.** Each provider submission recorded for a job, so retries are included. Mock requests cost $0.
- **Estimates, not invoices.** Account discounts, promotions and unlisted surcharges are not reflected. fal.ai labels the minimax per-second rates as launch prices (50% off until 30 Sep 2026); check them.

**Session detail now shows:**
- user status (active, exhausted, inactive, removed…);
- AI cost for the session and for the user in total;
- the selected image, the 360° movie, and the 3D model: Hi3D's thumbnail first, then an interactive three.js viewer of the light preview;
- a pipeline flow with each step's duration, requests and cost;
- 3D results in cc and cm², with a material colour dot, plus "what was done to the model" (repair, bore, scale, alignment, measurement);
- the customer's last Customize choice (full history on request);
- the journey, at the end.

**The Sessions list** shows the user status and *Has* chips (Image / Movie / 3D) for each session.
