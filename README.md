# XJet Jewelry Builder — Pipeline 3

A leaner ring-design flow:

**four images → select (with zoom) → refine the selected image into four more, or proceed → Customize with the static image plus a Minimax camera-controls movie → fixed 1 cm³ Fashion pricing / preview-only Luxury.**

There is no Visual Hull and no measurement. Hitem3D/STL is developer-only.

This repository is independent of the commercial Pipeline 2 (`XjetJewelryBuilder`). It shares no code, data, configuration, or deployment with it.

- Spec (planning baseline): [docs/PIPELINE_3_SPEC.md](docs/PIPELINE_3_SPEC.md)
- What was built, the decisions made, and the open items: [docs/IMPLEMENTATION.md](docs/IMPLEMENTATION.md)
- Pricing rules and what still needs approval: [docs/PRICING.md](docs/PRICING.md)
- Accounts, tokens and credits, and the future shared-auth integration point: [docs/ACCOUNTS.md](docs/ACCOUNTS.md)
- Admin area (user / token management, per-user activity and usage): [docs/ADMIN.md](docs/ADMIN.md) — `/JewelryB2C3/admin/`

## Setup (Windows, Python 3.13)

```bash
py -3.13 -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt
cp .env.example .env        # then edit .env
```

## Run locally (mock provider — no network, no cost)

The default is `P3_PROVIDER=mock`. For a usable local price, add these to `.env`:

```
P3_PRICING_PROFILE=config/pricing_profile.dev-example.json
P3_ALLOW_UNAPPROVED_PRICING=true
P3_ADMIN_KEY=<any long random string, to enable /dev>
```

Then issue yourself an access token and start the server:

```bash
.venv/Scripts/python -m p3.cli create-token --label "me"
.venv/Scripts/python -m uvicorn p3.app:App --port 8310
```

Open http://localhost:8310, choose **Sign in → Enter it here**, and enter the token. Alternatively, register with an email: in the default `P3_MAIL_MODE=outbox` the verification email is written to `var/outbox/` (and listed at `/api/dev/outbox`) instead of being sent. Developer mesh tools are at `/dev` (they require `P3_ADMIN_KEY`).

### With or without the `/JewelryB2C3` base path

| `.env` | Site | API / static / assets / dev |
|---|---|---|
| `P3_BASE_PATH=/JewelryB2C3` (deployment layout; recommended locally too) | http://localhost:8310/JewelryB2C3/ — `/` and `/JewelryB2C3` redirect there | `/JewelryB2C3/api/…`, `/JewelryB2C3/static/…`, `/JewelryB2C3/assets/…`, `/JewelryB2C3/dev` |
| unset / empty | http://localhost:8310/ | `/api/…`, `/static/…`, `/assets/…`, `/dev` |

With a base path, nothing is served at the root. That matters behind proto, where root `/api/`, `/static/`, `/admin/` and `/debug` belong to Pipeline 2. The server injects the prefix into the pages (`<meta name="p3-base">`), every browser request goes through one prefixed helper, and every URL the API returns already includes the prefix. `tests/test_base_path.py` fails if any URL escapes it.

## Mock vs. live mode

The Home screen always shows the mode as a **Mock Mode** or **Live AI** pill. Every screen also shows the amber banner (mock) or the green chip (live):

- **Mock** (default): an amber "Mock mode" banner plus a **Mock** chip in the nav. Images, movies and meshes are simulated placeholders, nothing is sent to any AI provider, and nothing is charged.
- **Live**: a green **Live AI** chip in the nav. Requests go to fal.ai and are billed.

`GET /api/health` reports `"mode"` and `"mode_source"` outside production (in production it says only `ok`; the details are at `GET /api/admin/health` with the admin key).

**Developer switch (internal).** Click **Developer** in the Home footer and enter `P3_ADMIN_KEY`; a panel then shows **AI Mode: Mock** or **AI Mode: Live → Switch to Mock**.
- Switching to Mock is one click.
- Switching to Live opens a billing warning and requires typing the exact confirmation sentence. It needs `FAL_KEY` in the server configuration.
- Switching is refused while generations are running.
- Customers never see this panel; the API behind it (`/api/dev/mode`) requires the developer key.

**What wins at startup:**
1. The last developer switch, saved in `var/runtime.json`.
2. Otherwise `P3_PROVIDER` from `.env`.

A saved "live" choice without a `FAL_KEY` starts in mock and says so. To go back to following `.env`, delete `var/runtime.json`.

## Going live (paid)

1. In `.env`, set `P3_PROVIDER=fal` and `FAL_KEY=<key>`. Ideally use a **Pipeline 3 key with a spending limit**, not the one Pipeline 2 uses.
2. Check the key without running any model. This makes a free 1×1 storage upload:
   ```bash
   .venv/Scripts/python -m p3.cli check-provider
   ```
3. Restart the server (it does not auto-reload), then confirm the nav shows **Live AI**.

Costs at published fal.ai rates:
- Each image batch or refinement is four Nano Banana Pro requests, about $0.60.
- Each Proceed on a new candidate is one Minimax request (price not published).
- Each developer mesh is about $2.10.

See docs/IMPLEMENTATION.md §4 before running live.

## Tests

```bash
.venv/Scripts/python -m pytest
```

All provider calls in the tests are mocked; no test uses the network.

## Production

`P3_ENV=production` makes the service refuse to start with development defaults and locks the live provider, hides the
developer tools, the API docs and the showcase, and serves the Admin only on `P3_ADMIN_HOST`. The complete variable
list is `.env.production.example`; the installation is `deploy/production/README.md`; the status of everything that is
still a decision or a credential is `docs/PRODUCTION-READINESS-HANDOFF.md`. Credits (1 per design request, 1 per
refinement request, 1 per extra option, 1 per movie), the daily AI spend cap and the request limits are described in `docs/ACCOUNTS.md` and
`docs/ADMIN.md`.

## Runtime data

The SQLite databases and generated assets live in `var/` (gitignored), or wherever `P3_DATA_DIR` points. Identity is kept apart from application data: `var/accounts.db` holds accounts, hashed tokens and usage, and `var/pipeline3.db` holds designs, batches, movies and the bag. Customer assets are in `var/assets/`. Developer meshes are in `var/dev/` and are never served statically.
