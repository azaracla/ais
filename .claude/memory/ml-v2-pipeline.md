---
name: ml-v2-pipeline
description: ML v2 pipeline architecture — time-to-arrival prediction from AIS data
metadata:
  type: project
---

Three-stage ML pipeline to predict time-to-arrival for vessels without declared ETA.

## Pipeline stages

1. **`ml/detect_arrivals.py`** — Reconstruct actual arrival times (ground truth)
   - Pass 1: Export ShipStaticData (ETA+destination) + positions from remote DuckLake catalog → local Parquet
   - Pass 2: Match destinations to UN/LOCODE ports (3-tier dict: LOCODE → name_port → name_any, 99% hit rate)
   - Speed-based detection: SOG < 0.5kn for ≥30min via DuckDB window functions (58,269 arrivals)
   - Geofence detection: ≤10km port radius + SOG ≤1kn (12,877 arrivals)
   - Merge: geofence priority, speed fallback → 71,146 arrivals → `arrivals.parquet`

2. **`ml/build_dataset.py`** — Sample positions + compute features
   - Target: `time_to_arrival_hours = arrival_ts - pos_ts` (no ETA dependency)
   - Sampling at 11 time horizons: [0.5, 1, 2, 3, 6, 12, 24, 48, 72, 120, 168] hours before arrival
   - DISTINCT ON (mmsi, arrival_ts) → closest position to each target horizon (±5min tolerance)
   - Features: dist_to_dest_km (vectorized haversine), sog, cog, bearing_offset_deg, vessel_length/width, length_width_ratio, ship_type, hour_of_day, day_of_week
   - Filters: dist 0-20000km, tta 0-200h, sog > 0 and < 50
   - Output: 198,366 rows → `dataset.parquet`

3. **`ml/train.py`** — XGBoost regression
   - Temporal split (80/20), no data leak
   - 300 estimators, max_depth=6, lr=0.05, subsample=0.8, colsample_bytree=0.8
   - Test MAE: 23.1h, R²: 0.08

## Key results
- Best performance at medium horizons (6-24h TTA → 12.0h MAE, <50km → 12.9h MAE)
- Model over-predicts systematically (53.4% of errors >10h late)
- Top features: dist_to_dest_km (16.1%), sog (14.6%), day_of_week (12.3%)

## Coverage
- 68% of 165K active MMSIs have no declared ETA → model fills this gap
- Model works for any vessel with a known destination, regardless of ETA declaration

**Why:** ML pipeline for AIS ETA prediction, critical for understanding model limitations.
**How to apply:** Run scripts in order: detect_arrivals → build_dataset → train. Use model to predict ETA for vessels without declared ETA.
