# Pipeline 3 fashion pricing

## Confirmed rules (product owner)

- Every Fashion unit (Stainless Steel, Silver, Vermeil) is priced at an assumed volume of **1 cm³**.
- The selected Fashion material determines the price.
- Ring size never changes the price.
- Luxury (gold) shows **"Price unavailable"** and cannot be added to the bag.

These are enforced by `p3/pricing/service.py` and `p3/customize.py` on the server. The browser only displays the result.

## Unresolved: the numbers themselves

The Pipeline 2 CPP cost formula needs **bounding-box dimensions as well as volume**. Tray packing (parts per tray) and print time (layer count) depend on the dimensions. A fixed 1 cm³ volume does not define them, and **no reference dimensions or fixed price table have been approved.**

The effect is not small. With the CPP formula at 1 cm³ (values from the P2 browser calculator, reproduced exactly by this repo's port):

| Material | 22×22×8 mm box | 18×20×3 mm box |
|---|---|---|
| Stainless Steel | $112.87 | $85.12 |
| Silver (Sterling 925) | $271.80 | $240.66 |
| Vermeil (plating not costed) | $297.68 | $263.58 |

Therefore:

- **Shipped default** (`config/pricing_profile.json`): every Fashion material is `unconfigured`, so the API returns `pricing_status: "unavailable"` and Add to Bag stays disabled. This is deliberate.
- **Development example** (`config/pricing_profile.dev-example.json`): uses the CPP formula with an **illustrative, unapproved** 22×22×8 mm reference box. The server honours it only with `P3_ALLOW_UNAPPROVED_PRICING=true`, and every quote and bag line is labelled "Unapproved development pricing".

## What the product owner needs to decide

Pick one of these:

1. **Fixed price table.** A unit price per Fashion material, plus a note on where it came from:
   ```json
   {"version": "fashion-2026-10", "approved": true, "currency": "USD", "assumed_volume_cm3": 1.0,
    "materials": {
      "stainless_steel": {"method": "fixed", "unit_price": 0.0, "source": "..."},
      "silver":          {"method": "fixed", "unit_price": 0.0, "source": "..."},
      "vermeil":         {"method": "fixed", "unit_price": 0.0, "plating_cost_usd": 0.0, "source": "..."}}}
   ```
   (Zero prices are rejected. Replace them with real values.)
2. **CPP with approved reference dimensions.** Set `method: "cpp_reference"` and an approved `reference_dims_mm` (and optionally `cpp_shop_parameters`), then set `approved: true`.

For either option, **Vermeil plating must be stated explicitly** (`plating_cost_usd`; 0 is allowed). An approved profile without it makes Vermeil unavailable. P2 never costed plating.

To activate a profile, point `P3_PRICING_PROFILE` at the approved file. Each quote carries `pricing_version` (profile version plus content hash), and bag lines snapshot it.

## Quote contract

`GET /api/quote?material_id=silver&ring_size=7` (ring_size is echoed and ignored):

```json
{"material_id": "silver", "material_group": "fashion", "pricing_status": "available",
 "unit_price": 271.8, "currency": "USD", "assumed_volume_cm3": 1.0,
 "pricing_version": "dev-example-cpp-2026-09-29+p-10d21a484660", "profile_approved": false,
 "estimated_weight_g": 10.36, "unavailable_reason": null, "notes": ["..."]}
```

Luxury, or an unavailable Fashion price, returns `pricing_status: "unavailable"` and `unit_price: null`. `unavailable_reason` is one of: `luxury_pricing_unavailable`, `pricing_profile_unapproved`, `pricing_not_configured`, `pricing_profile_invalid`, `plating_cost_not_configured`. There is never a fallback to zero, a catalog price, or a luxury price.

`estimated_weight_g` is `1.0 × density`. It is an estimate from the assumed volume, not a measured weight.

## Provenance of the CPP data

`p3/pricing/cpp_db.json` is an independent snapshot of Pipeline 2's `constants.js` merged with `cpp-overrides.js` and `metal-map.js` (revision `1e871734`, overrides dated 2026-09-14). Only the material rows Pipeline 3 uses are kept. One change from P2: P2's Python port hard-coded the tray size, while the browser used the tray row. This port uses the tray row, matching the browser. The parity values are in `tests/test_pricing.py`.

## Charms (separate from everything above)

Everything above is ring pricing and is unchanged. Charms have their own price book, edited in Admin → Settings →
Pricing & Materials → **Charm** (`p3/charmprices.py`, table `charm_price_lists`, versions `charms-v<N>`):

- **A fixed price per material *and* size.** A charm's size is its total height, including the attachment loop at
  the top, and the sizes are set in Admin → Settings → Products. Unlike a ring, a charm's size can change its price.
- **Materials.** Stainless Steel, Sterling Silver and 14K Gold Vermeil have fixed prices. Gold uses the quote flow,
  like gold rings.
- **Price and cost per gram** for the 3D calculated price and the production cost of a charm.
- **No invented numbers, no fallback.** The table starts empty. A missing price makes the charm "Price unavailable":
  never the ring price, never zero, never a calculated number.

Quote: `GET /api/quote?material_id=silver&product=charm&charm_size=20`. It is only answered while charms are visible
to that browser; otherwise it returns `400 unknown_product`, as if charms did not exist.

```json
{"material_id": "silver", "material_group": "fashion", "pricing_status": "available", "unit_price": 145.0,
 "currency": "USD", "assumed_volume_cm3": null, "pricing_version": "charms-v2", "profile_approved": true,
 "estimated_weight_g": null, "unavailable_reason": null, "notes": ["..."], "product": "charm", "charm_size": 20.0}
```

`unavailable_reason` for a charm is one of:
- `charm_size_required`: no size chosen yet;
- `charm_size_not_offered`;
- `charm_price_not_set`;
- `luxury_pricing_unavailable`: gold, which uses the quote flow;
- `material_not_offered`.
