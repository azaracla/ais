---
name: ml-v2-improvements
description: Concrete improvements to reduce v2 MAE from 23h toward <12h
metadata:
  type: project
---

## Root causes of high MAE (23.1h)

1. **XGBoost can't learn ratios** — `dist / sog` is the naive ETA formula. Trees approximate this poorly with splits. The model must rediscover division from scratch.
2. **Single-position snapshot** — no trend data (accelerating? decelerating? changing course?)
3. **No route context** — haversine distance ignores coastlines, straits, traffic separation schemes
4. **ship_type as raw integer** — not one-hot encoded. XGBoost treats it as continuous, missing categorical distinctions
5. **No per-MMSI speed profile** — some vessels consistently faster/slower than type average

## Priority improvements

| # | Action | Expected MAE impact | Effort |
|---|--------|---------------------|--------|
| 1 | Add `dist_to_dest / max(sog, 1)` as explicit feature | -5 to -10h | Trivial |
| 2 | One-hot encode `ship_type` | -2 to -3h | Trivial |
| 3 | Historical speed features: avg SOG over last 1h/6h/24h before each sample | -3 to -5h | Medium |
| 4 | More samples at short horizons (<6h) — oversample where model is most useful | Better short-range accuracy | Low |
| 5 | Per-MMSI average speed as feature (from all positions) | -2h | Low |
| 6 | Try LightGBM or MLP instead of XGBoost | -1 to -3h | Low |
| 7 | Add `heading` feature (true_heading from AIS positions) | Marginal | Low |

## Quickest win

Feature `eta_naive = dist_to_dest_km / max(sog, 1)` alone should beat the current model. The model currently has to learn this simple division through tree splits — which XGBoost is fundamentally bad at.

**Why:** Actionable roadmap to fix the model's poor performance.
**How to apply:** Start with #1 (eta_naive feature), retrain, compare. If MAE drops below 15h, proceed to #2 and #3. Skip #7 if bearing_offset already captures heading info.
