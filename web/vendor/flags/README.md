# Country flags

The flags of the shipping countries (`p3/addressing.py` `Countries`), one 4:3 SVG per country, named by its ISO 3166-1
alpha-2 code in lower case. They show in Checkout's country and phone-code pickers.

- Source: flag-icons 7.2.3 (https://github.com/lipis/flag-icons), `flags/4x3/<code>.svg`, from
  `https://cdn.jsdelivr.net/npm/flag-icons@7.2.3/` (4 Nov 2026), unchanged.
- Licence: MIT, `LICENSE` beside this file.
- Each file was checked before it was added: an `<svg>` root, no script, `foreignObject`, event handler or reference
  outside the file (`tests/test_checkout_fields.py` checks it again).
- A new shipping country needs its flag here, from the same version.
