# Pipeline 3 — implementation record

Date: 2026-09-29. Planning baseline: [PIPELINE_3_SPEC.md](PIPELINE_3_SPEC.md) (the handoff) and [PIPELINE_2_FLOW.md](PIPELINE_2_FLOW.md) (P2 reference trace).

This document separates three things:

- **Confirmed** — agreed product requirements (spec §3).
- **Recommendation** — implementation defaults chosen here. These are *not* product decisions and can be changed.
- **Open** — decisions or verifications still needed.

> **Product-owner changes, 2026-09-30** (they supersede the matching items below):
> - **Batch size.** Every batch — the first generation and every refinement — now produces **four** candidates, not six. The grid is 2×2 and sized to fit the first screen. `images.candidates_per_batch = 4` is enforced in `p3/config.py`, and refinement still uses the selected image for every output. Cost is about $0.60 per batch at published rates. Four is also the provider's `num_images` maximum, but P3 keeps one request per slot so each slot keeps its own seed, status, and retry.
> - **Movie is the default Customize view.** The "360° Movie" tab is first and selected on entry. While the movie is being made, or if it failed, the static image stays visible with a status or retry pill. The "Image" tab is second.
> - **Customize final behaviour (confirmed).** The 360° Movie tab is first and selected on entry, and a ready movie plays immediately. While the movie is being made there is no empty or blocked state: the selected image stays visible with a small status pill, and all controls stay usable.
> - **Design waiting screen.** P2's waiting movie (`atelier-loading.mp4`, copied to `web/videos/`) plays inline in the Design screen, with P2's "design" / "refine" titles and rotating status lines. It shows while a new design or a refinement is generated, then the four results replace it. Unlike P2 it is not a full-screen overlay, so the nav, My Designs, and batch tabs stay usable.
> - **UI caching.** `/`, `/dev` and `/static/*` are served with `Cache-Control: no-cache`, so browsers revalidate (304) instead of running an outdated `app.js` after an update.
> - **Luxury order.** Row 1 is 10K / 14K / 18K Yellow Gold; row 2 is 10K / 14K / 18K Rose Gold (`config/materials.json` order plus a fixed 3-column grid).

> - **Accounts boundary (2026-10-01).** Identity, tokens and usage moved behind `p3/accounts` into their own `accounts.db`. Application rows store `owner_account_id`, and a credit check runs before every paid action. This replaces the token-based access described in §3. See [ACCOUNTS.md](ACCOUNTS.md) for the future shared-auth integration point with P2.

> - **Phase 1, base path and AI mode (2026-10-01).**
>   - `P3_BASE_PATH=/JewelryB2C3` mounts the whole app under the prefix. `/JewelryB2C3` redirects (308) to `/JewelryB2C3/`, and nothing is served at the root.
>   - Pages get the prefix injected, browser requests go through one prefixed helper, and asset URLs from the API include the prefix. `tests/test_base_path.py` guards this.
>   - The AI provider mode has a developer-only runtime switch (`/api/dev/mode`, a Home footer panel), persisted in `var/runtime.json`. Live mode requires a typed cost confirmation, and switching is refused while jobs run.
>   - Reconciliation only resumes requests that belong to the current provider. Live requests found in mock mode wait for live mode.

## 1. Isolation from Pipeline 2

- This is an independent Git repository with its own remote. Nothing here imports, links to, or reads Pipeline 2 at runtime.
- Pipeline 2 (`C:\Users\yakir.tubul\Git\XjetJewelryBuilder`) was read only, at revision `1e871734bb9ac0f824bdb8950d62fbf6a35083bb`. Nothing was edited or installed there, and its backend was not run. Its two pre-existing untracked items (`PIPELINE_2_FLOW.md`, `RingRescaler/`) were fingerprinted before work began and re-verified unchanged afterwards (§9).
- One read-only step touched P2 files: P2's browser calculator (`xjet-calc.js` and its data files) was evaluated under Node from a scratch directory to produce parity reference values. No files were written into P2.
- The following were copied into this repo as independent files, after review:
  - the two Nano Banana system prompts and the P2 prompt suffix → `config/prompts/`
  - a subset of the CPP constants, overrides, and metal map → `p3/pricing/cpp_db.json`
  - material colour tints/swatches and the ring-size list → `config/materials.json`
  - the fal error classification idea and the CPP formula → re-implemented
- Not copied: secrets, key files, databases, generated assets, logs, job JSON, customer data, deployment/nginx scripts, VisualHull, or the classifier.
- Runtime data lives only under `P3_DATA_DIR` (default `./var`, gitignored). The dev server runs on port **8310**.

## 2. Confirmed requirements → where they live

| Requirement | Implementation |
|---|---|
| One prompt → six different images | `p3/images.py`: each batch has 6 candidate rows (slots 0–5) sharing one `effective_prompt`, each with a distinct seed |
| Select one; clear rectangular border | `web/index.html` `.card.selected` (3 px solid outline + "Selected" tag). Selection is persisted server-side (`PUT /api/designs/{id}/selection`) |
| Zoom | Separate zoom button per card → lightbox with click-to-magnify. Zoom never toggles selection |
| Refine / Proceed / Start new | Action bar. Refine and Proceed are disabled until a ready image is selected |
| Refine → six new variations of the selected image | `CreateRefinement`: the selected image is uploaded once and used as `image_urls[0]` for all six edit requests, with one identical instruction |
| Customize image = selected image | `POST /api/designs/{id}/customize` returns the selected candidate's `image_url`. The UI shows it before the server responds |
| Customize video via `minimax/h3-max/camera-controls` | `p3/movies.py`, endpoint constant in `p3/providers/endpoints.py` |
| Default material Silver | `config/materials.json` (`default_material_id` is validated as `silver`) |
| Fashion group: Stainless Steel, Silver, Vermeil | `config/materials.json` |
| Luxury group: gold options | The six golds P2 offered in Customize (10K/14K/18K, yellow and rose). **No other luxury materials were invented.** |
| Fashion pricing at fixed 1 cm³ | `p3/pricing/service.py`. Numbers are **Open**; see [PRICING.md](PRICING.md) |
| Luxury "Price unavailable"; Add to Bag disabled | Enforced by the quote service and `CustomizeService.AddToBag` (HTTP 409), and shown in the UI |
| Standard ring size; size never changes price | US 4–12 in half sizes. Size is not a pricing input (see tests) |
| No Visual Hull, no automatic measurement | Neither exists in this codebase |
| Developer-only 3D/STL via `hitem3d/hi3d/v3.0/image-to-3d` | `p3/meshes.py` behind `P3_ADMIN_KEY` (`/api/dev/*`, `/dev` page) |

## 3. Implementation recommendations adopted (not product decisions)

- **Six requests per batch, one image each.** The provider caps `num_images` at 4 (see §4), so a batch is six single-image requests, each with its own seed. This gives per-slot identity, retry, and status, and a failed slot never regenerates successful ones.
- **Concurrency.** Up to 6 image requests run at once server-wide (`images.max_concurrent_requests`), so one batch runs fully in parallel.
- **Duplicate outputs.** An exact duplicate within a batch (by SHA-256) is re-requested once with a new seed (`max_duplicate_retries_per_slot: 1`). A second duplicate marks that slot `failed/duplicate_output`, and the user can retry it.
- **Refinement presentation.** The current grid stays visible with a "Creating six refinements… n of 6 ready" line. When the new batch finishes, the view switches to it and the selection is cleared so the user chooses again. Earlier batches stay as tabs ("Original", "Refinement 1", …).
- **Partial batches.** Ready images can be selected while other slots finish or fail. The UI states "n of 6 designs are ready" and offers "Retry missing designs". A batch is only labelled `complete` at 6/6.
- **Movie does not gate price or bag.** The movie is a preview. A failed movie leaves the image, material, size, price, and Add to Bag usable, with a "Retry movie" action.
- **Material preview** reuses P2's CSS tint filters on the same image and movie, labelled "Metal colour is a visual preview of the same design". No paid regeneration happens on material change.
- **Selecting a group** reveals its options and selects that group's last-used option (Fashion → Silver by default; Luxury → 10K Yellow Gold first). The price is cleared immediately on switching, so a Fashion price is never shown for Luxury.
- **"Start new"** clears the active design, selection, and customization in the UI. Saved designs and the bag are kept.
- **Size must be chosen explicitly** before Add to Bag (no default size).
- **Quantity** is 1–10. Different sizes of the same design are separate bag lines at the same unit price.
- **Access.** Customer requests require an access token (`X-Access-Token`), issued by an operator via `python -m p3.cli create-token`. Unlike P2, anonymous generation is not allowed. Email self-registration and mail were not carried over.
- **Usage.** Each provider submission is recorded in `usage_events` (image = 1 per slot, movie = 1). **No quota is enforced** — see Open items.
- **Asset URLs** (`/assets/...`) are unauthenticated capability URLs built from random 128-bit IDs. Developer meshes are stored outside the public asset root and are served only through the authenticated download route.
- **Prompt validation** is length-only (3–2000 characters). P2's LLM prompt gate and ONNX ring classifier were **not** carried over (see Open items).
- **Reference upload** (optional) requires a rights-confirmation checkbox, as in P2. It is limited to PNG/JPEG/WebP up to 10 MB and re-encoded to PNG. With a reference, the initial batch uses the edit endpoint.

## 4. Provider verification (fal.ai OpenAPI, fetched 2026-09-29)

| Endpoint | Verified contract | How P3 uses it |
|---|---|---|
| `fal-ai/nano-banana-pro` | `prompt` (≥3 chars), **`num_images` 1–4**, `seed`, `aspect_ratio`, `resolution` 1K/2K/4K, `output_format`, `system_prompt`, `limit_generations`. Output `images[].url` | 6 requests × `num_images=1`, distinct `seed`, 1:1, 1K, png, P2's system prompt |
| `fal-ai/nano-banana-pro/edit` | As above plus required `image_urls[]` | Refinement, and initial generation with a reference |
| `minimax/h3-max/camera-controls` | **Exists.** `image_url` (first frame), `prompt_expansion_mode` (`disabled`/`balanced`/`quality`), `duration` 3–15 s (int), `resolution` `480P`/`768P`/`1080P`, `camera_trajectory` = 2–12 keyframes `{time 0–1, azimuth°, elevation −90..90°, distance>0}`, `seed`. Output `video.url` | `duration 6`, `768P`, expansion `disabled`, a rigid-scene orbit prompt, 5 keyframes 0→360° azimuth at 10° elevation, distance 1.0 |
| `hitem3d/hi3d/v3.0/image-to-3d` | `image_url` (PNG/JPEG/WebP ≤ 20 MB), `resolution` `2048quality`/`2048master`, `face_count` 100k–5M, **`export_format` glb/obj/stl/fbx/usdz**, `enable_texture`, `enable_pbr`, `shading`. Output `model_mesh.url` | `export_format: "stl"` by default (native STL, no conversion). GLB/OBJ can be converted with trimesh as a separate step |

**Discrepancies and cautions:**

- The spec's warning was correct: `num_images=6` is not accepted (maximum 4).
- The Minimax `camera_trajectory` values are a **recommendation, not validated live**. In particular, the meaning of `distance: 1.0` ("normalized scene units") and how well a 360° orbit preserves a ring on white are unverified.
- The fal OpenAPI lists `prompt_expansion_mode` as required, but it has a default. P3 always sends it.
- None of Veo's parameters (`aspect_ratio`, `negative_prompt`, `generate_audio`, 8 s/720° prompt) are sent to Minimax. A test asserts this.

**Published costs** (fal model pages, 2026-09-29; fal notes pricing may change):

- Nano Banana Pro: $0.15 per image at 1K/2K, so **about $0.90 per six-image batch** and $0.90 per refinement (P2 used 1 image per generation).
- Hi3D v3.0: about $2.10 at the quality tier, billed per credit (texture +10 credits, PBR +5; both are disabled here).
- **Minimax H3 Max camera-controls: no price shown — Open.**

## 5. Architecture

```
web/index.html + app.js      customer UI (Alpine.js), server-authoritative, stale-response guards
web/dev.html                 developer mesh tool (bearer P3_ADMIN_KEY)
p3/app.py                    FastAPI routes; startup reconciliation
p3/images.py                 six-slot batches, refinement lineage, dedupe, slot retry
p3/movies.py                 selected-image Minimax job, dedupe by (candidate, config version)
p3/customize.py              selection → customization → quote → bag (server-enforced rules)
p3/meshes.py                 developer Hitem3D single-image mesh, STL
p3/pricing/                  quote service + CPP port + CPP data snapshot
p3/providers/                fal adapter, mock provider, endpoint ids
p3/runner.py                 task registry + one bounded polling contract
p3/db.py                     SQLite schema (own file under var/)
p3/assets.py                 containment-checked paths, atomic writes, content validation
config/                      generation params, prompts, catalog, pricing profiles
```

**Persisted objects** (`p3/db.py`): `access_tokens`, `designs` (selected_candidate_id), `batches` (kind initial/refine, parent_candidate_id, user_text, effective_prompt, endpoint, reference, config_version), `candidates` (slot, seed, status, provider_request_id, asset, sha256, error), `customizations` (one per design+candidate: material, size, quantity), `movies` (candidate, config_version, provider_request_id, status), `meshes` (candidate, settings, provenance, original/STL paths), `bag_lines` (quote snapshot), `usage_events`.

**Statuses:**

- candidate: `pending / generating / ready / failed`
- batch (derived): `queued / generating / complete / partial / failed`
- movie and mesh: `queued / running / ready / failed / interrupted`

**Job contract** (`p3/runner.py`):

1. Submit to the fal queue and persist `provider_request_id` immediately.
2. Poll status at `P3_POLL_INTERVAL_S` until complete, within a per-kind deadline (images 300 s, movie 900 s, mesh 1800 s).
3. Transient read errors retry the *same* request up to `P3_MAX_TRANSIENT_POLL_ERRORS` consecutive times.
4. Fetch the result, download it, validate the content (image decode / MP4 `ftyp` / STL or GLB structure), then write atomically (temp file + rename).
5. Only then mark the job `ready`.

**Restart reconciliation** (on startup):

- A job with a persisted `provider_request_id` resumes polling. It is not resubmitted.
- A job without one (possibly submitted just before a crash) is marked `failed/interrupted` (candidate) or `interrupted` (movie/mesh) and waits for an explicit user retry. **Paid work is never resubmitted automatically.**

**Late results:** workers write only their own candidate, movie, or mesh row. Nothing asynchronous changes `designs.selected_candidate_id`. The UI ignores poll responses for any design or batch other than the one they were issued for.

**Cache keys:** movies are reused per `(candidate_id, movie config_version)`. A partial unique index allows only one queued/running/ready movie per key, which deduplicates repeated Proceed clicks and races. `config_version` is a content hash of `config/generation.json` sections and the prompts, so editing them invalidates reuse automatically.

## 6. API (all JSON; errors are `{"error": {"code", "message"}}`)

| Method & path | Purpose |
|---|---|
| `GET /api/health` | Provider mode, pricing profile version/approval, config versions |
| `GET /api/session` | Token label and usage counts |
| `GET /api/catalog` | Groups → materials, default, ring sizes |
| `GET /api/quote?material_id&ring_size` | Fixed-volume quote (size ignored) |
| `POST /api/designs` (multipart `prompt`, `client_request_id`, optional `reference` + `rights_confirmed`) | New design + initial six-image batch (idempotent per client_request_id) |
| `GET /api/designs`, `GET /api/designs/{id}` | Saved designs; full state for reload recovery |
| `POST /api/designs/{id}/batches` `{parent_candidate_id, instruction, client_request_id}` | Six-image refinement of the selected candidate |
| `GET /api/batches/{id}` | Batch + candidates |
| `POST /api/batches/{id}/retry-failed`, `POST /api/candidates/{id}/retry` | Retry failed slots only |
| `PUT /api/designs/{id}/selection` `{candidate_id}` | Persist selection (ready candidate of this design) |
| `POST /api/designs/{id}/customize` `{candidate_id}` | Proceed: customization (Silver default) + ensure movie |
| `GET/PATCH /api/customizations/{id}` `{material_id?, ring_size?, quantity?}` | Customize; response carries quote, `can_add_to_bag`, reason, movie |
| `POST /api/candidates/{id}/movie` | Start/reuse movie; after failure, a new attempt |
| `GET /api/bag`, `POST /api/bag {customization_id}`, `DELETE /api/bag/{line}` | Server-validated bag of quote snapshots |
| `GET /api/gallery`, `GET /api/gallery/{item}/share`, `POST /api/gallery/{item}/start` | Gallery tiles (public), the share link by design name, Make it yours |
| `GET /api/favorites`, `PUT/DELETE /api/favorites/{design_id}` | ♥ Favorites of the signed-in account (references to gallery masters) |
| `GET /thumb/{asset}?w=&f=`, `GET /poster/{movie}` | Cached thumbnails and movie posters (see 8b) |
| `GET /api/showcase?design=` | Homepage hero showcase data (read-only) |
| `GET /clip/{movie}?tail=2&w=720` | A movie's last seconds as a small web clip (cached) |
| `/api/dev/*` (Bearer `P3_ADMIN_KEY`) | `status`, `candidates`, `candidates/{id}/meshes`, `meshes`, `meshes/{id}`, `meshes/{id}/convert-stl`, `meshes/{id}/download?kind=stl\|original` |

**Checkout:** there is none. The bag reports `checkout_available: false` and states that no order, payment, or production is created. The P2 coupon/reservation screen was not copied.

## 7. Removed relative to Pipeline 2

The following do not exist here in any form, including as dormant calls:

- Visual Hull and its jobs/caches
- bore measurement and ring rescale
- `ringMeasuredDiameter`/`ringBaselineGeo`/`analysisReady`/`open_ring`
- cardinal/quadrant extraction and padding
- multi-view Hitem3D
- `/api/analyze-part` geometry pricing
- size-driven `sf³` pricing
- Veo video, the JPEG scrub-frame pipeline, and fallback catalog prices

The P2 ring classifier was not copied, because it imports VisualHull. The consequence for validation is listed in Open items.

## 8. Open items and decisions needed

1. **Fashion price numbers** — fixed table, or approved CPP reference dimensions, plus the Vermeil plating cost. Until then, Fashion shows "Price unavailable" in the shipped configuration. See [PRICING.md](PRICING.md).
2. **Minimax cost** per video (not published on the model page), and **live validation** of the camera trajectory, duration, and resolution. The chosen values are recommendations.
3. **Batch charging/quota policy.** Six images per batch and per refinement cost about 6× a P2 generation. P3 records usage but enforces no limits. A policy is needed before public use; P2's "one credit per video" was deliberately not reinterpreted.
4. **Prompt/ring validation.** P2's LLM gate (config absent from the P2 checkout) and ONNX ring classifier (VisualHull dependency) are not in P3. Only length validation plus the Nano Banana system prompt constrain outputs. Decide whether an independent validator is needed.
5. **Registration.** Email self-registration was not carried over; tokens are operator-issued.
6. **Checkout/order integration** — out of scope by spec §12; requires a separately identified system.
7. **Video gating.** P3 does not require the movie before Add to Bag (recommendation). Confirm.
8. **Luxury list** — the six P2 golds. White gold and 24K were not offered in P2 Customize and were not added.
9. **Live performance** — no latency or quality claims are made until measured with live providers.
10. **Uploaded reference URL lifetime** — fal storage URLs are reused when a failed refinement slot is retried later. If fal expires them, the retry fails with a provider error, and a new refinement re-uploads.

## 8a. UI parity with Pipeline 2 (second iteration, 2026-09-29)

At the product owner's request, the customer UI was rebuilt to look like P2. The P3 logic above is unchanged.

- **Visual system:** copied from `index-p2.html` — Tailwind (a CDN then; a self-hosted production build since 2026-10-03, see 8b), Inter/Cinzel, the `btn-gold`/`btn-black`/`btn-outline` system, contrast fixes, and tap-target sizing. The nav, footer, and marketing imagery (`Angel.JPG`, `Ink.JPG`, `ISO.png`, `PrintHead.png`, favicons) were copied into `web/images/`.
- **Pages ported:** Home, Inspiration, Materials (now grouped Fashion / Luxury and driven by the catalog), Technology, FAQ, About XJet, Shipping & Returns, Terms, Privacy, Contact.
- **Design screen:** follows P2's studio — "Hello, Designer." landing, gold-bordered composer with reference upload, prompt bubble, typing-dots generating state, the My Designs sidebar with search, and the fullscreen zoom/pan preview. P3 differences:
  - a 3×2 grid of six cards (2 columns on phones);
  - a square-cornered black selection border with a "Selected" tag;
  - "Select This Design" inside the preview;
  - batch tabs (Original / Refinement n);
  - three actions (Start a New Design · Refine Selected · Customize Your Ring);
  - once a design exists, the composer refines the selected image, and reference upload is offered only for new designs.
- **Customize screen:** follows P2's review panel — left preview (P3: Image / 360° Movie toggle with the Minimax video instead of the scrub canvas), the Metal Type list, the 44 px size buttons with P2's size-guide modal (limited to P3's US 4–12), configuration summary, price, quantity, "In Your Bag", and Back. P3 differences:
  - metals are grouped into expandable **Fashion Jewelry** rows and a **Luxury** gold grid marked "Preview only";
  - the price shows "Price unavailable" for Luxury or unconfigured pricing;
  - the Add to Bag label explains why it is blocked;
  - there is no measurement progress, `open_ring`, or per-piece size list (separate bag lines are used instead).
- **Bag:** P2's "My Bag" step layout, showing server bag lines. It has no shipping, payment, coupon, or reservation steps; "Checkout Unavailable" is shown disabled.
- **Product names:** deterministic local naming in `p3/naming.py` (the idea of P2's `generateProductName`, with a new two-word vocabulary): "Aurora Twist", "Fil Wave", "Serpent Scale" — family word + descriptor from the prompt's keywords, unique across designs, no AI call. See docs/ADMIN.md → Names.
- **Mode indicator:** amber Mock banner + chip, or a green Live AI chip, driven by `/api/health` `mode`. The developer page shows the same.

**Copy changed from P2** (P2 statements that are not true of P3, or were unverifiable):
- "Preview your design in 3D" / "approve a full 3D model" → "choose from six designs… preview it in a 360° movie"
- "Estimated prices… may change once your ring has been measured" → removed
- "each 360° preview uses one generation" → removed
- "Secure Checkout / protected payment" trust tile and FAQ → replaced; there is no checkout
- Terms "Orders" → states that ordering is not yet available
- Privacy → references access tokens instead of email registration
- The **Customer Reviews section** (three named five-star testimonials) was **not carried over**, because it presents testimonials as genuine and they could not be verified. Restore it only with real, attributable reviews.
- `SUPPORT_EMAIL` stays `atelier@xjet3d.com`, which P2 marks "TODO confirm".

**Not ported** (not relevant to P3 or requiring separate decisions):
- choice/signature collections, AI-configure and debug steps, and the dev picker shortcuts
- Visual Hull / measurement UI and the Hitem3D customer screens (mesh stays on `/dev`)
- email registration and verification, quota coin, favourite/delete in My Designs
- shipping/payment/reservation checkout

**Live-mode hardening found during this iteration:**
- Uploads now declare the stored image's real MIME type. Live outputs may be JPEG/WebP; everything was previously declared `image/png`.
- A fal request that completes with an error string (e.g. `content_policy_violation`) is now classified. It was previously a generic `provider_error`.
- An HTTP 402 is now recognised as billing.
- A startup warning appears when `P3_DATA_DIR` is long enough that asset paths could exceed the Windows 260-character limit. This actually happened during verification with a deep temp directory.

## 8b. Delivery, self-hosting, sharing, favorites, showcase (2026-10-03)

- **Self-hosted front-end:** no page loads anything from a third-party host. Tailwind is a production build — `tailwind.config.js` + `web/tailwind.src.css` → `web/vendor/tailwind.css`; rebuild after changing classes in the pages or scripts:
  `npx -y tailwindcss@3.4.17 -c tailwind.config.js -i web/tailwind.src.css -o web/vendor/tailwind.css --minify`
  (`tests/test_vendor.py` fails when a class used in a page is missing from the build). Inter and Cinzel are WOFF2 files with `web/vendor/fonts.css`; Alpine 3.13.3 and three.js 0.128 (+ STLLoader, OrbitControls) are in `web/vendor/`. All of them get `?v=<mtime>` like `app.js`.
- **Image and movie delivery** (`p3/media.py`): `GET /thumb/<asset>?w=320|800[&f=webp|jpg|png]` — a thumbnail made on first request from the original and cached under `var/assets/_derived` (WebP when the browser accepts it, else JPEG; never upscaled). Small views use it (gallery tiles with a `srcset`, hero strip, My Designs, the four options, bag lines, Materials page, Admin lists — the Admin hover preview still enlarges the original); large views, zoom and downloads use the original. `GET /poster/<movie>` — the movie's first frame as a JPEG (ffmpeg from the `imageio-ffmpeg` wheel; without it the movie simply has no poster). `/assets`, `/thumb`, `/poster` and versioned static files are cached for a year (their URLs never change content); pages revalidate. Text responses are gzip-compressed (level 6); media and the 3D downloads are not.
- **Sharing:** `GET /api/gallery/{item}/share` (public) → `{slug, title, text, url}`; `GET /design/{slug}` serves the site with the Open Graph / Twitter tags of that design and opens its preview. `designs.share_slug` is set once (first share) and never changed.
- **Favorites:** table `gallery_favorites (owner_account_id, design_id, created_at)`; `GET /api/favorites`, `PUT/DELETE /api/favorites/{design_id}` (only current gallery masters; idempotent). Public gallery tiles carry `design_id` so the heart can refer to the master. Entry points: `openPanel('designs'|'favorites')` in `web/app.js` (header icons, phone menu, account panel, studio toolbar); signed out it remembers the tab, asks for sign-in and opens it right after (`_pendingPanel`). Header: `nav.site-header` is one CSS grid (rules in the page's style block) in three arrangements, so every control exists once — phones: one row (`brand nav util cta`); the site's pages from 768 px: `util util util` / `brand nav cta` (the utility row: My Designs · ♥ · bag · account; the main row: brand · How It Works, Inspiration, Materials, About ▾ · Start Designing; the pages are in the menu below 1024 px); the Design screen (`.is-studio`): one row. The account menu (the former account panel, teleported, `z-[400]`) is placed under the header's account button by `_placeAccountPanel()` and ends with Sign out; `openBag()` asks for sign-in first when signed out (`_pendingPanel = 'bag'`). Below 1024 px the side panel is a drawer at z-60 over the sticky header (z-50).
- **Metal filters** live in `web/metal.js` (`P3MetalFilterDefs(materials)`), shared by the site and the showcase; the output is byte-identical to the former in-component getter.
- **Homepage hero showcase** (approved 2026-10-03): the hero's right side is the animated story of one real gallery design (Aurora Mesh, `design: 'aurora-mesh'`; another story is chosen automatically if it leaves the gallery) — Your idea → Four possibilities → Make it yours → Every angle → Your finish: its prompt typed, its first four options, the one chosen ("Selected"), its refinement request typed and the refined ring revealed by a soft wipe, its 360° movie settling on the front view, Silver and 18K Yellow Gold on the same still (same angle and scale), and the finished ring in gold with only its name and "Designed with XJet Atelier", held about 3.5 s. About 16 s, muted, loops; no call to action inside it (the hero's buttons are beside it). Engine `web/showcase.js` + `web/showcase.css`: every movement is computed from one clock, so nothing overlaps; the clock stops while the stage is off screen or the tab is hidden; reduced motion shows the finished ring still. Data: `GET /api/showcase[?design=<link name>]` (`p3/showcase.py`, read-only; the render's own metal is shown untouched, Silver on a gold render uses a contrast curve that keeps depth and highlights). The movie is played as `GET /clip/<movie>?tail=2&w=720|480` — its last two seconds as a small H.264 clip made once with the bundled ffmpeg (about 0.15 MB instead of 8 MB). Square stage on a computer, 4:5 on a phone, where it follows the headline and the buttons. `/showcase` stays as the reference page (the same engine, with a design picker, Phone preview, timeline and storyboard; not linked, noindex).

## 8c. Rings and charms — product types (2026-10-05)

Charms are a second product beside rings, each with its own configuration and behaviour (`p3/products.py`). The ring
implementation of before is the reference: its tests are unchanged and must keep passing.

- **Product type on every design** (`designs.product_type`, `'ring'` by default, so every existing design, session and
  order is a ring without being rewritten). It is set when a design is created (`POST /api/designs`, form field
  `product`; none = ring) and never changes: a database trigger refuses any update and any value other than ring or
  charm. Refinements and forks (`images.CreateRefinement`), split refinements (`merge.SplitRefinement`) and gallery
  journeys inherit the product; a legacy-copy merge never joins a ring and a charm.
- **Separate customer-facing IDs**: rings keep `designs.ring_no` (R-1001 …, unchanged); charms have their own sequence in
  `designs.charm_no` (C-1001 …), each assigned by its own trigger for its own product only, each with its own retired
  numbers (`retired_rings`, `retired_charms`). `ringids.Ref(row)` gives the ID of either product; option IDs follow
  (C-1003-B, C-1003-R1B).
- **Bag and order lines** carry `product_type` and `charm_size` next to `ring_size`; a CHECK keeps every ring line with a
  ring size and every charm line with a charm size (the guarantee the former NOT NULL gave rings). Both tables were
  rebuilt once (rows, ids and values copied unchanged, atomically). `order_lines.purchase_json` snapshots the purchased
  configuration (product, size with its label and — for a charm — what the size measures, material, price).
  `customizations.charm_size`, `quote_requests.product_type` / `charm_size` were added as columns.
- **Customer visibility** (Admin → Settings → Products, OFF by default): while charms are hidden the customer site and
  its API are the ring-only ones of before — no charm tile, favorite, share page, design or "Make it yours"; gallery
  tiles keep exactly their former fields. A browser signed in to the Admin previews charms (`products.CharmsVisible`).
- **Admin**: sessions, session pages, orders (`GET /api/admin/orders?product=ring|charm`), order pages, the user page,
  the gallery list and the dashboard (`by_product`) show the product with its icon (`web/products.js`: a band with a
  stone, a neutral pendant on a loop — no emoji) and filter All · Rings · Charms; a mixed order says so.

**Phase 2 — the charm AI configuration and the customer switch.**

- **Models per product** (`p3/modelconfig.py`). Every `ModelSpec` has a `Product`.
  - Charms have four models of their own: `nano-banana-pro-charm`, `nano-banana-pro-edit-charm`,
    `minimax-camera-charm` and `hi3d-charm`. They use the same endpoints and parameter definitions (shared constants),
    with their own versions, active pointers and history.
  - `ByEndpoint` still maps to the ring models only. `ModelFor[(endpoint, product)]` / `ModelIdFor` and
    `ActiveFor(endpoint, product)` choose by product.
  - `images._InsertBatch` reads the design's product, and `_Arguments` builds the request from the recorded version's
    model. Movies (`movies._Model`) and meshes (`meshes.Create`) pick the candidate's product the same way.
- **Seeding.**
  - Ring v1 seeding (`_SeedMissing`, `_SeedInternalDefaults`) is untouched and runs for ring models only.
  - `_SeedCharms` then creates each missing charm model's v1 from the Ring model's *active* provider settings plus the
    charm prompts. `CharmSeed` copies values, never a reference to a ring version.
- **Charm prompts** (`config/prompts/charm_image_generate_system.txt`, `charm_image_edit_system.txt`,
  `charm_image_suffix.txt`, `charm_movie_prompt.txt`; `config/generation.json` → `charm`).
  - They were written separately in the house style of the ring prompts, and the ring prompt files are unchanged.
  - Every charm has one plain, round, closed loop at the top centre, in the plane of the front face, about a fifth of
    the body height, so it reads immediately as jewelry. The loop is not a creative feature, and there is no loop
    detection, validation or manufacturing rule.
  - Inconsistencies found in the ring prompts are written up in `docs/RING-PROMPT-NOTES-2026-10-05.md` and not fixed.
- **Names** (`p3/naming.py`).
  - Charms use the same rules with `Product="charm"`. Their framing words (charm, pendant, chain, necklace, bail,
    loop, bracelet) are removed from the prompt and never appear in a name, and the ring-only descriptors (Open/Cuff,
    Midi/Pinky) are skipped.
  - Ring names are byte-identical to before. A snapshot of 75 prompts was compared before and after the change, and
    tests cover it.
- **Customer availability** (`products.ProductSettings`, Admin → Settings → Products).
  - `PUT /api/admin/products/availability` turns charms on or off; it refuses to turn them on when the charm
    configuration is incomplete (`Supports("charm")`). Changes are logged in `product_settings_log`. (The typed
    phrase of the first version was removed on 2026-10-05: the Admin confirms in a normal dialog.)
  - `/api/catalog` gains a `products` block (available products, default, admin `preview` flag, charm sizes and the
    size definition) only when charms are visible to that browser, and is served `no-store`.
- **Mock provider.** A charm request (its system prompt says "jewelry charm") gets a placeholder charm image, so the
  charm flow can be tested without a paid call.
- **Tests:** `tests/test_charm_configuration.py`. It checks:
  - seeding;
  - charm saves that never touch the ring configuration, and the reverse;
  - per-product exports;
  - the full charm pipeline in Admin preview: generation, refinement directives, the movie, and a fork keeping charm
    with its own C- number;
  - the switch, including its confirmation, the catalog block and customer creation only while ON;
  - charm names and unchanged ring names.

**Phase 3 — charm sizes and pricing.**

- **Charm sizes**: `PUT /api/admin/products/charm-sizes` with `products.ValidateCharmSizes` (3–100 mm, at most 12,
  unique) and a change log. The response says how many bag lines still hold a removed size.
- **Price book** (`p3/charmprices.py`).
  - `CharmPriceBook`: versioned `charm_price_lists` (`charms-v<N>`) with, per charm material, fixed prices per size
    plus price and cost per gram. It is seeded empty.
  - `Validate` refuses gold fixed prices, non-positive numbers, sizes outside 3–100 mm and materials a charm is not
    made in. Every charm material keeps a row.
  - `QuoteFor(material, size)` never reads the ring pricing.
  - `charmprices.QuoteFor(Ctx, product, material, charm_size)` is the single dispatch: rings go to
    `Ctx.Pricing.QuoteFor` exactly as before.
  - `MaterialLabel` gives charm names (Sterling Silver, 14K Gold Vermeil). `Price3D` is for the Phase 4 charm 3D path.
- **Customize** (`p3/customize.py`).
  - A charm opens without a size and refuses ring sizes; a ring refuses charm sizes.
  - Purchasability for a charm is checked size first (`charm_size_required`, `charm_size_not_offered`), then price.
  - The charm JSON adds its sizes, each with today's price in the chosen material, the size definition and its
    materials.
  - Bag lines are quoted by (product, material, charm size), so staleness is per product.
- **Checkout and orders** (`p3/orders.py`).
  - A charm line's problems are worded for charms, with the size checked before the price.
  - Order events and quote requests carry `product_type` / `charm_size` for charms. A gold charm's quote request
    validates its charm size. Ring code paths and texts are unchanged.
- **Sessions.** `FixedPrice` and `QuoteSnapshot` take the product. Charm summaries add `charm_size` and
  `charm_size_chosen` (a charm has no default size); ring summaries keep exactly their former fields.
- **Admin UI.**
  - Settings → Products has the charm sizes editor.
  - Pricing & Materials has *Material pricing for: Ring | Charm*: the ring table unchanged, and the charm table with
    one fixed-price column per size, "Quote" for gold, and price and cost per gram.
  - The session page shows a charm's size in mm and its material as the customer saw it.
- **Tests:** `tests/test_charm_pricing.py`. It checks:
  - the empty start with no ring fallback;
  - prices per material and size;
  - validation;
  - sizes edited and enforced;
  - charm customization rules;
  - a mixed ring and charm bag, order, snapshot and email;
  - repricing isolated per product;
  - gold charm quote requests;
  - charm quotes hidden while charms are hidden.

**Phase 4 — the charm 3D path.**

- **`p3/charmgeometry.py`** (`charm-measure-once-v1`).
  - `MeasureCharmRaw`: thickness = least principal spread; height = model up (Z) projected into the face plane,
    falling back to the longest direction when the model lies flat; extents in that frame; volume, area and the
    closed heuristic from `geometry._Moments`.
  - `CharmScaled`: s = height target / measured height.
  - `ExportScaledCharmStl`: lying flat, centred, mm, header `XJet P3 scaled charm`.
  - The ring functions in `p3/geometry.py` are unchanged.
- **`p3/production3d.py`** branches on the design's product.
  - `Request` uses `_CharmSize` (customer size, else `products.CharmDefaultSize`, any 3–100 mm by the Admin) and
    charm materials.
  - `_Continue` measures a charm mesh with `product: "charm"`.
  - `_FinalizeCharm` writes the raw and production rows with no inner diameter. The whole charm, loop included, is
    scaled to the size (the size is the total height). The status is `measured`, or `needs_review` only when the
    closed-mesh heuristic disagrees.
  - `_Price` uses `Ctx.CharmPrices` (`Price3D`, version `charms-vN`) and the charm quote for the produced size and
    material.
  - `RepriceMissing` uses each product's own book, and `CharmPriceBook.OnSave` triggers it.
  - `StartExport` passes the height as `target_mm`.
  - `Get` adds `product_type`, `target_height_mm` and `size_label` for charms. `FileName` gives `…_20mm.stl`.
- **`p3/geometry_worker.py`**: `measure` and `export` take `product: "charm"`. Ring jobs run exactly as before.
- **Admin.**
  - The session detail gives a charm its 3D defaults (customer size or the middle size) and a catalog of charm sizes
    and charm materials.
  - The 3D panel has a charm size select (mm), a *Height* card, charm labels and `adjustmentsCharm`.
  - Order lines say "Latest result: 25 mm".
- **Mock provider.** A mock charm image carries a PNG text marker (`p3mock=charm`). A Hi3D request for it returns an
  upright mock charm: a disc body with a plain loop on top, Z-up. Rings keep the torus.
- **Tests:** `tests/test_charm_3d.py`. It checks:
  - measuring once and scaling by height;
  - the review item;
  - charm prices;
  - the STL export and its file name;
  - sizes, defaults and materials;
  - repricing from the charm book only;
  - the unchanged ring path beside a charm;
  - an ordered charm line;
  - the up-direction frame on synthetic models.

**Corrections of 2026-10-05.**

- **Charms available to customers** is a simple switch with a normal confirmation dialog (no typed phrase).
- **any-llm · Charm** (`any-llm-charm`) completes the Charm side of AI models & prompts: every Ring model has a Charm
  counterpart. The Any-LLM parameters are one shared definition (`_AnyLlmParams`); the settings are separate. Its
  v1 = the Ring Any-LLM's active settings, copied by value, plus `config/prompts/charm_anyllm_system.txt`.
- **Charm size = total height including the loop** (decision of 2026-10-05). It is central in
  `products.CharmSizeDefinition` (measure `total_height`, `includes_loop`, label, short and full text) and
  `products.Charm3DHeight` (the height the 3D model is scaled to). It is used by Customize (the definition text),
  order snapshots and emails, the 3D scaling and the STL header (`XJet P3 scaled charm 20 mm total height incl.
  loop`). The blanket review item for charms was removed. At start-up, `Production3D.ClearLegacyLoopReviews` marks
  as `measured` the charm results that the first path had flagged only for the loop in the height (their numbers
  already follow the total height); a result with any other problem keeps its flag.
- **Promo messages** are product-neutral: "Add a piece to your bag before using a promo code." and "This promo code
  applies to Silver pieces only."
- **AI models & prompts editor.** Alpine 3.13.3 does not clean up template blocks (`x-if` / `x-for`) nested inside a
  removed `x-for` row or `x-if` block: their bindings kept running against later models and threw console errors
  (tens to hundreds per switch, growing with every switch).
  - The parameter rows are now fixed slots (`paramSlots`, as many as the largest model has) that persist across
    models. Each slot reads its parameter live from the current model, and the kind editors use `x-show`, never
    `x-if`.
  - Keyframe cells are written out, and all draft reads are null-safe. The placeholder list and the
    refinement-directive preview use `x-show`.
  - Verified with an error counter across every model, Ring → Charm → Ring, saving, loading a version into the form,
    discarding, restoring, previews and parameter edits: no console errors.

**Phase 5 — the customer experience** (`web/app.js`, `web/index.html`; `web/products.js` for the icons).

- **Gating.** Everything below is shown only while the catalog has its `products` block (`productsOn`), that is,
  while charms are visible to that browser. With charms hidden the page is the ring-only site of before.
- **Ring-only answers** (`app.RingOnly`). While charms are hidden from a browser, and an answer holds no charm, these
  answers carry no product fields (`product_type`, `product_types`, `charm_size`, `size_label`), exactly as before the
  charms work:
  - designs (list and one);
  - the bag;
  - checkout;
  - orders;
  - quote requests.

  A customer whose bag or orders hold charms (made while charms were shown) still gets those answers whole. The
  gallery and favorites already behaved this way (phase 1).
- **Design screen.**
  - "What would you like to design? Ring / Charm" replaces the "makes rings" note, with Ring as the default.
  - The choice (`newProduct`) is kept in local storage, so it survives sign-in, including through the emailed link.
  - The composer reads "Describe your charm…", and `generate()` sends `product` only while products are offered.
  - My Designs marks a charm.
- **Customize for a charm** (`custIsCharm`).
  - A *Choose your charm size (height)* section shows the size definition and one button per size with its price
    ("Unavailable" when it has no price, "Quote" for gold). It replaces the ring size and size guide, which are
    unchanged for rings.
  - Materials use the charm names. A charm has no suggested size: a chosen one counts as confirmed.
  - The price, summary, bag-button and gold-quote texts are charm-worded, and a quote request sends `charm_size`.
- **Bag, checkout, confirmation, My Orders.**
  - `lineSize` gives "US 7" (as before) or "20 mm", and `lineIdLabel` gives Ring ID / Charm ID.
  - Order counts read "2 rings", "1 charm" or "3 pieces".
- **Inspiration Gallery.** A *All · Rings · Charms* filter (`galleryShown`) on the gallery page and dialog, and a
  "Charm" badge on charm tiles. The homepage showcase and its gallery strip are unchanged.
- **Copy.** The FAQ (what you can create, price, sizes) and the Terms service sentence name rings and charms only
  while products are offered.
- **Admin preview.** A small fixed indicator ("Admin preview · charms are hidden from customers") on tablet and
  desktop, and a line in the product choice on a phone, while `catalog.products.preview` is true.
- **Tests:** `tests/test_charm_customer.py`. It checks:
  - ring-only answers while hidden, and the product fields in Admin preview;
  - a customer holding charms keeps them after charms are hidden;
  - the page wiring.

  Two phase 1 tests now expect the ring-only answers while hidden.

## 9. Verification evidence

**Automated** (`pytest`, 50 tests; **all provider calls mocked** by `p3/providers/mock.py`; no network):

- six distinct candidates from one prompt with distinct seeds and `num_images=1`
- token required
- idempotent create
- reference upload requires rights confirmation and uses the edit endpoint
- selection ownership
- refinement uses the selected image bytes for all six with one instruction; lineage and prior batch preserved
- missing reference → 409, no text-only fallback
- partial failure → slot and batch retry without regenerating ready slots
- total failure retry
- duplicate re-request and bound
- transient poll retry and bound
- designs do not cross-contaminate; no auto-selection
- restart: 5 resumed, 1 interrupted, zero resubmissions
- path containment
- Proceed shows the selected image; exactly one Minimax call using that image; no Veo params; no mesh call
- repeated Proceed dedupes; movie failure keeps image and price; retry calls only the movie endpoint
- size invariance; quantity affects the line total only
- Luxury clears the price and gets 409 from the bag; switching back restores the Silver price
- bag requires size and an available quote; unapproved/unconfigured profile blocks the bag; owner scoping; reload recovery
- pricing: CPP port equals the P2 browser calculator to 1e-9 (6 cases); dimension sensitivity; unconfigured/unapproved/zero/invalid profiles → unavailable; Vermeil plating rule
- developer routes: disabled without key, 403 with a wrong key or customer token; single-image STL mesh with provenance; GLB→STL conversion; invalid settings rejected; mesh failure leaves the customer flow intact

**Manual UI check** (mock provider, local only, port 8310):

- sign-in
- six-image grid with loading states
- selection border kept after zoom
- refinement progress, then the new batch tab
- Proceed → Customize with the static image, then the movie auto-shown when ready
- Luxury → "Price unavailable" with Add to Bag disabled
- back to Silver with the price restored; size change leaves the price unchanged
- Add to Bag
- developer endpoints via curl (403 without or with a wrong key; native STL download)

Late in the session the preview pane stopped painting (`requestAnimationFrame` never fired), so the final bag screen was confirmed through the page state rather than a screenshot.

**Live adapter wiring** (`tests/test_fal_adapter.py`): runs the real `FalProvider` through the whole app with only fal's network client object faked. It checks:
- the exact arguments sent to all four endpoints
- queued/in-progress/completed status mapping
- 503 and connection errors retried on the same request without resubmission
- completed-with-error → per-slot `content_policy` failure
- 402 → billing, 422 → permanent

The fal client's queue URLs for the three nested endpoints were also resolved offline and are correct (`queue.fal.run/{owner}/{app}/requests/{id}`).

**Not exercised:** no live fal.ai call of any kind was made. Live compatibility, output quality, latency, and cost remain unverified. Live validation needs a Pipeline 3 test key with a spending limit and an explicit test budget.

**Pipeline 2 integrity:** checked at the end of implementation (see the final section of the session report). HEAD `1e871734`, status still shows only the two untracked items, and their SHA-1 fingerprints are unchanged.
