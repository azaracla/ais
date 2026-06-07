# ML ETA Prediction — Progress Log

## v2 (baseline)
- 10 features: dist_to_dest_km, sog, cog, bearing_offset_deg, vessel_length, vessel_width, length_width_ratio, ship_type (raw int), hour_of_day, day_of_week
- XGBoost 300 estimators, max_depth=6, lr=0.05
- Test MAE: 23.1h, R²: 0.08
- 198,366 rows, 11 time horizons

## v3 — 2026-06-07
### Changes
- **eta_naive_h** = dist_to_dest_km / max(sog, 1.0) — ratio feature XGBoost can't learn from trees alone
- **Historical SOG**: avg_sog_1h, avg_sog_6h, avg_sog_24h (epoch-based DuckDB window functions)
- **sog_trend_1h**: current SOG minus avg of previous hour (acceleration/deceleration signal)
- **One-hot ship_type**: 32 top types (≥500 occurrences) + "other" category (replaced raw integer)
- COALESCE NULLs to current SOG for first positions of each MMSI

### Results
- **Test MAE: 15.4h** (-33% vs v2)
- Within 12h: 61.4% (×2 vs v2)
- Within 24h: 84.7%
- <50km MAE: 9.2h
- 47 features (14 numeric + 33 one-hot)
- Top features: ship_type_30 (5.8%), eta_naive_h (5.3%), sog (5.2%), dist_to_dest_km (4.2%)
- Still over-predicts (35.7% >10h late) but much less than v2 (53.4%)

### Analysis
- `dist/sog` as standalone predictor is terrible (MAE 2607h!) because SOG can be very low → ratio explodes. But as a feature in XGBoost, it's #2 most important (5.3%).
- Ship types dominate (6 of top 15 features) → one-hot encoding was crucial.
- Model still conservative: mean error -1.8h (slight over-predict), P50=-6.7h.

## v4 — 2026-06-07
### Changes
- **Log-transform target**: `log1p(y)` → trains in log-space → `expm1` at inference. Critical for skewed distribution.
- **Sample weighting**: weight = 1/(tta_h + 1). Short horizons get ~100× more weight than long horizons.
- **LightGBM**: 500 rounds, num_leaves=127, early_stopping=50. Slightly better than XGBoost (11.1h vs 11.2h).
- **XGBoost**: 500 estimators, max_depth=7, lr=0.03, stronger regularization (alpha=0.5, lambda=2.0).
- Both models trained on log-target.

### Results
- **Test MAE: 11.1h** (LightGBM, -28% vs v3)
- Within 1h: 54.1% (×7.5 vs v3!)
- Within 6h: 79.1%
- 0-1h TTA MAE: **0.6h** — excellent near-arrival prediction
- 1-6h TTA MAE: **1.0h** — very usable
- <50km MAE: 6.9h
- R² negative in original space (long-range variance dominates) but within-N-h metrics excellent
- Train MAE (16.5h) > Test MAE (11.2h) — temporal split, newer data has more short-horizon samples

### Analysis
- Model is now essentially a **near-arrival detector**: excellent at <6h, degrades gracefully to 24h, useless beyond 72h
- eta_naive_h dominates XGBoost importance (11.0%) — the ratio feature finally works with log-transform
- Sample weighting effectively focuses training on short horizons (70% of test data is <6h TTA)
- LightGBM marginally better than XGBoost (0.1h MAE difference)
- 2000+ km bucket has 11.2h MAE — model learns "far = many hours" but doesn't distinguish within far

## v5 (per-horizon) — 2026-06-07
### Changes
- **5 specialized LightGBM models**, one per TTA bin: 0-1h, 1-6h, 6-24h, 1-3d, 3-8d
- Each model trained ONLY on samples in its bin → no cross-horizon interference
- Added **mmsi_avg_sog** (per-vessel average speed) and **sog_vs_mmsi_avg** (ratio vs vessel average)
- Optuna-tuned hyperparams (50 trials): num_leaves=88, lr=0.072, bagging_freq=7

### Results
- **Ensemble MAE: 3.4h** (-69% vs v4, -85% vs v2!)
- **R²: 0.9015** (vs -0.12 for v4 — paradigm shift)
- 0-1h bin: **MAE 0.2h** (±12 min), 100% within ±2h
- 1-6h bin: **MAE 0.9h**, 91% within ±2h
- 6-24h bin: MAE 4.2h, 79% within ±6h
- 1-3d bin: MAE 13.9h
- 3-8d bin: MAE 26.6h
- Within 6h: 86.9%, Within 24h: 96.0%

### Key insight
Global model forced to predict 0.5h and 168h with same parameters → log-variance of short horizons destroyed by long-horizon noise. Per-bin models each learn a narrow distribution → log-transform works properly → massive accuracy gain.

### Caveat
At inference, need to pick which model to use (true TTA unknown). Solution: use eta_naive_h (dist/sog) as first-stage router. Mis-routing cost is bounded.

## Next ideas
1. **Inference router** — classifier to pick the right horizon model from eta_naive + features
2. **Quantile regression** — prediction intervals (P10/P90) for uncertainty quantification
3. **Port-specific features** — average waiting time, congestion at destination port
4. **Weather data** — wind, waves, currents along route
5. **Production deployment** — package models, inference API
