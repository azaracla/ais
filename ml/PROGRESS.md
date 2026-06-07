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

## Next ideas (ordered by expected impact)
1. **Log-transform target** — target distribution is right-skewed (0.4h to 168h). Log-transform → RMSE in log-space → exp back. Should reduce long-range error.
2. **LightGBM** — native categorical support, often better on tabular data, faster training.
3. **Hyperparameter tuning** — Optuna or randomized search on n_estimators, max_depth, lr, subsample.
4. **Sample weighting** — weight short-horizon samples more (they're more useful in practice).
5. **Distance traveled features** — distance between current position and 1h/6h ago (effective speed, not just instantaneous SOG).
6. **Port congestion / time of day** — some ports have rush hours, night closures.
7. **Cross-validation** — K-fold temporal instead of single 80/20 split.
