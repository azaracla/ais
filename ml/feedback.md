# ML ETA Prediction — External Feedback & Analysis

## Source 1: Gemini — 2026-06-07

### Strengths identified
- **Leakage catch is critical**: spotting temporal split had same-vessel leakage, switching to MMSI-grouped split — prevents catastrophic production failure
- **Two-stage residual architecture is elegant**: avoids fragile classifier routing, Stage B implicitly corrects Stage A's macro-errors. R² jump -0.03→0.58 validates this
- **Adaptive blending**: sigmoid transition at 3h threshold recovers short-horizon accuracy

### Critiques
- **Short-horizon degradation (0-24h)**: even with adaptive blending, 0-1h (2.4h) and 1-6h (3.9h) worse than v6 (0.6h, 1.0h). Caused by MMSI split penalty — model can't memorize vessel-specific micro-behaviors
- **Feature bloat**: adding 20+ features only yielded 9% single-stage gain. Physics baselines should do heavier lifting
- **Feature collinearity**: `eta_naive_h` and `eta_phys_6h` likely collinear — LightGBM splits importance, muddying interpretation

### Recommendations
| Priority | Action | Rationale |
|---|---|---|
| High | Train Stage B only on hard samples (Stage A error > 3h) | Forces Stage B to be long-horizon specialist |
| Medium | Haversine correction factor per port pair | Route distance without external API |
| Low | More trees / CatBoost tuning | Architectural changes > hyperparameter tuning |

---

## Source 2: Second detailed analysis — 2026-06-07

### What Claude/Gemini missed

#### 1.1 Replace 32 one-hot `ship_type` with LightGBM native categorical
```python
df['ship_type'] = df['ship_type'].astype('category')
lgb.Dataset(X, label=y, categorical_feature=['ship_type', 'nav_status'])
```
**Impact**: −0.5 to −1h MAE. One-line change. LightGBM handles categorical natively with optimal binary partitions — one-hot trees must grow deeper for same accuracy.

#### 1.2 Target encoding out-of-fold for MMSI and port
Current `mmsi_avg_tta` and `port_avg_tta` computed on full dataset = slight leakage. Fix: 5-fold out-of-fold target encoding.
```python
kf = KFold(n_splits=5)
for tr_idx, val_idx in kf.split(df):
    means = df.iloc[tr_idx].groupby('mmsi')['tta_h'].mean()
    mmsi_te[val_idx] = df.iloc[val_idx]['mmsi'].map(means).fillna(global_mean)
```

#### 1.3 Interaction features (missing)
```python
df['dist_x_closing'] = df['dist_to_dest_km'] * df['closing_speed_kmh']
df['weighted_efficiency'] = df['approach_efficiency'] * np.log1p(df['dist_to_dest_km'])
df['mmsi_tta_bias'] = df['mmsi_avg_tta'] / (df['eta_naive_h'] + 1e-3)
df['sog_vs_hist'] = df['avg_sog_1h'] / (df['mmsi_median_sog'] + 0.1)
```
**Rationale**: These are the splits trees would learn if given enough depth — encoding explicitly accelerates convergence.

#### 1.4 AIS freshness (completely absent)
```python
df['ais_age_h'] = (snapshot_time - df['last_ais_timestamp']).dt.total_seconds() / 3600
df['ais_stale'] = (df['ais_age_h'] > 2).astype(int)
df['ais_age_x_dist'] = df['ais_age_h'] * df['dist_to_dest_km']
```
A vessel without recent AIS = potentially anchored, poor coverage, or stale data. Meijer showed AIS quality significantly impacts ETA accuracy.

#### 1.5 Anchoring zone detection
```python
df['anchoring_suspected'] = (
    (df['avg_sog_1h'] < 0.5) & 
    (df['dist_to_dest_km'] > 10) & 
    (df['dist_to_dest_km'] < 80)
).astype(int)
df['stationary_duration_h'] = df['stop_fraction_6h'] * 6
```
Most direct lever for 6-24h horizon improvement. Vessels anchor in fixed zones for 12-48h before port entry.

### Architecture optimizations

#### Stage B: best filtering condition
Filter on `eta_naive_h > threshold` (not Stage A error — circular). Test thresholds: [3, 6, 12, 24].
```python
for threshold in [3, 6, 12, 24]:
    mask_b = (df_train['eta_naive_h'] > threshold)
    model_b.fit(X_train[mask_b], residuals_train[mask_b])
```

#### Log-transform target: why v4 failed, how to fix
v4 failed because no sample weight adjustment. Fix:
```python
model.fit(X, np.log1p(y))
y_pred = np.expm1(model.predict(X_test))
weights = 1.0 / np.log1p(y_train + 1)  # balance short/long horizons
```

#### DART: dropout for trees
On datasets with collinearity (85 features, many redundant), DART generalizes better:
```python
params = {'boosting_type': 'dart', 'drop_rate': 0.1, 'skip_drop': 0.5, 'max_drop': 50}
```
Caveat: DART doesn't support classic early stopping — use fixed validation split.

### Stacking: proper meta-learner
Baltic Sea AIS study achieved MAPE 0.25% with Extra Trees + AutoGluon + LightGBM + Ridge meta-learner.
- Ridge assigns LGBM weight +0.984, XGBoost weight −0.187
- **Explains why naive ensemble (average) was worse**: XGBoost hurts LGBM in unweighted ensemble

```python
oof_lgbm = cross_val_predict(lgbm, X, y, cv=mmsi_kfold)
oof_xgb = cross_val_predict(xgb, X, y, cv=mmsi_kfold)
meta_X = np.column_stack([oof_lgbm, oof_xgb])
meta_model = RidgeCV(alphas=[0.01, 0.1, 1, 10]).fit(meta_X, y)
```

### Horizon-weighted loss (structural problem)
Optimizing flat MAE causes model to sacrifice long horizons (few samples, large error) for short horizons (many samples, already excellent).

```python
def horizon_weighted_mae(y_true, y_pred):
    weights = np.ones(len(y_true))
    weights[y_true < 1] = 0.5       # 0-1h: already excellent
    weights[(y_true >= 1) & (y_true < 6)] = 0.8
    weights[(y_true >= 6) & (y_true < 24)] = 1.0
    weights[(y_true >= 24) & (y_true < 72)] = 2.0  # 1-3d: needs improvement
    weights[y_true >= 72] = 3.0     # 3-8d: hardest, most penalized
    return 'horizon_mae', np.average(np.abs(y_pred - y_true), weights=weights), False
```

### Plan of experiments (8 runs)
```
Run 1: LGBM native categorical (ship_type + nav_status) — clean baseline
Run 2: Run 1 + interaction features (dist_x_closing, mmsi_tta_bias, sog_vs_hist)
Run 3: Run 2 + ais_age_h + anchoring_suspected
Run 4: Run 3 + Stage B filtered on eta_naive_h > 12h
Run 5: Run 4 + target log-transform + inverse weights
Run 6: Run 5 + 10k trees, LR=0.01, early_stopping=200, lambda_l1/l2=1.0
Run 7: Run 6 + DART instead of GBDT
Run 8: Stacking Ridge (LGBM Run 6 + ExtraTrees + RFR meta)
```

### Horizon 3-8d: three fundamental limits
1. **Haversine vs route distance**: 15-25% error at 2000km (islands, straits). Fix: empirical route distances from 97M historical positions
2. **Slow steaming + terminal windows**: vessels adjust speed for berth slots. Fix: empirical trip duration distribution per port pair from 71K arrivals
3. **AIS signal quality at long range**: SOG at 5 days is barely predictive of 5-day average SOG. Only behavioral signals help (has this MMSI done this route before?)

### Overall impact estimates
| Lever | Impact | Effort | Priority |
|---|---|---|---|
| Stage B specialized + deep convergence | −1 to −2h | Low | ★★★ |
| Replace one-hot with LightGBM native categorical | −0.5 to −1h | Low | ★★★ |
| Interaction features + AIS freshness | −0.5 to −1h | Medium | ★★★ |
| Stacking with Ridge meta-learner | −0.5h | Medium | ★★ |
| Log-transform target + weight correction | −0.5 to −1h | Medium | ★★ |
| DART instead of GBDT | −0.3h | Low | ★★ |
| Anchoring detection upstream | −1 to −3h on LH | Medium | ★★ |
| Route distance (empirical from 97M positions) | −5 to −10h on 3-8d | High | ★ |
| Weather / currents | −2 to −5h on 3-8d | Very high | ★ |

---

## Source 3: Perplexity — 2026-06-07

### Key insight
Gap between 10.4h (global) and 3.3h (oracle) is an **information problem**, not a modeling problem. When horizon is known, model is excellent.

### Highest-leverage directions
1. **Continuous latent horizon** — two-stage model, Model A predicts coarse ETA, Model B predicts residual
2. **6h trajectory features** — heading drift, cumulative turn, stop/go patterns, trajectory straightness
3. **Port behavior modeling** — median waiting time, congestion proxy, anchorage likelihood
4. **MMSI-level priors** — typical cruising speed, slowdown distance, historical ETA bias
5. **Better physics baseline** — smoothed speed, penalize bearing_offset, approach_efficiency
6. **Quantile regression** — P10/P90 for uncertainty (not for MAE)
7. **What won't move the needle**: more tree tuning, more one-hot features, more routing tricks, weather

---

## Source 4: Nuanced retort — 2026-06-07

### Disagreement with Gemini
- Route distance NOT the highest priority now — **Stage B specialization + feature pruning** are higher ROI
- LightGBM supports early stopping, 10k trees, L1/L2 regularization, quantile objectives natively

### Stage B variants to test
- `B_easy`: Stage A < 3h, Stage B bypass
- `B_hard`: Stage A >= 3h, Stage B active
- `B_long`: true TTA > 6h or > 24h (alternative gating)

### Recommended execution order
1. Ablation Stage B hard-only with thresholds 3h / 6h / 24h
2. Long training run: n_estimators=10000, lr=0.01, early_stopping=200, stronger L1/L2
3. Pruning 20-30 weakest features
4. Quantile models for P10/P90
5. Route distance only if gains plateau on 3-8d

### Conclusion
- To break under 10h MAE reliably: **Stage B specialized + deeper convergence + pruning**
- To improve 3-8d significantly: **route distance + weather** (next tier after current approach plateaus)

---

## Synthesis: v8 Implementation Priority

From all four sources, converged on these top actions:

| # | Action | Source consensus | Effort | Expected gain |
|---|---|---|---|---|
| 1 | **LightGBM native categorical** (ship_type, nav_status) | All 4 | 1 line | −0.5 to −1h |
| 2 | **Horizon-weighted loss** | S2, S4 | ~20 lines | Structural fix |
| 3 | **Interaction features** (dist×closing, mmsi_tta_bias, sog_vs_hist, weighted_efficiency) | S2 | ~10 lines | −0.5 to −1h |
| 4 | **AIS freshness** (ais_age_h, ais_stale) | S2 | ~20 lines | −0.5h |
| 5 | **Anchoring detection** (from stop_fraction + distance) | S2 | ~5 lines | −1 to −3h on LH |
| 6 | **Stage B filtered on eta_naive > threshold** | S1, S2, S4 | Done in v7 | −0.5h |
| 7 | **10k trees, lr=0.01, L1/L2 stronger** | All 4 | Param change | −0.5h |
| 8 | **Target encoding OOF** (mmsi, port) | S2 | ~30 lines | −0.3h |
| 9 | **Log-transform target + corrected weights** | S2 | ~10 lines | −0.5h |
| 10 | **Stacking Ridge meta-learner** | S2 | ~40 lines | −0.5h |
| 11 | **DART** | S2 | 1 param | −0.3h |
| 12 | **Quantile regression P10/P90** | S1, S3 | 1 param | Utility, not MAE |
| 13 | **Empirical route distances** | S1, S2, S3 | Complex | −5 to −10h on 3-8d |
