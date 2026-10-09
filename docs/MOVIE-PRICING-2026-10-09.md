# Movie pricing review — 2026-10-09

**Endpoint:** `minimax/h3-max/camera-controls` (fal.ai, *H3 Max Camera Controls (Image to Video)*), used for every 360°
movie. **P3's request:** 6 seconds at 1080P (the active configurations for rings, `minimax-camera@v2`, and charms,
`minimax-camera-charm@v8`). Nothing about the movie changed: model, resolution, duration and when movies are made stay
as they are. No fal.ai API call was made for this review.

## Source

fal.ai's documentation of the endpoint (`https://fal.ai/models/minimax/h3-max/camera-controls/llms.txt`, read
2026-10-09): video costs $0.03 per second at 480p, $0.048 at 768p and $0.096 at 1080p — promotional rates, 40% off for a
limited time; the discount ends on 15 October, after which 480p is $0.05, 768p $0.08 and 1080p $0.16 per second. (fal.ai's
pricing page lists MiniMax H3 Max image-to-video at $0.05 per second, consistent with the post-promotion 480p rate.)

## Stored vs official

| Resolution | Stored until now (per s) | Official today (per s) | Official from 15 Oct 2026 (per s) |
|---|---|---|---|
| 480P | $0.025 | $0.03 | $0.05 |
| 768P | $0.04 | $0.048 | $0.08 |
| 1080P | $0.08 | $0.096 | $0.16 |

The stored rates were fal.ai's launch prices (50% off until 30 Sep 2026); they had expired.

## Impact

- **Per movie** (6 s at 1080P): estimated $0.48 until now → $0.576 today (+$0.096, +20%) → $0.96 from 15 Oct 2026
  (+$0.48, twice the old estimate).
- **Proto since 1 Oct 2026:** 51 movies recorded, 5 of them live (the others mock, $0): estimated $2.40 at the old
  rate, $2.88 at today's official rate. Estimates already recorded are not recalculated (each submission keeps the
  estimate made when it was sent); only new submissions use the new rates.
- **What uses the estimate:** the Admin's AI cost per session and per user, the dashboard, the daily spend tally, the
  AI spend warning and the hard cap (`P3_DAILY_AI_SPEND_CAP_USD`) where one is configured. With the old rates a
  movie-heavy day was under-estimated by 17% today and would have been by 50% after 15 Oct.

## What changed

- The price list now supports a dated change (`scheduled`), so the list moves to $0.05 / $0.08 / $0.16 by itself on
  15 Oct 2026.
- The code's default list (`p3/aipricing.py`) carries the official rates and the scheduled change; each site's stored
  list is updated through the Admin (*AI Prompts & Params → AI prices*) as a new version: proto and Atelier.
