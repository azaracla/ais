# ML ETA Prediction — Progress Log

## Summary of all versions

| Version | Approach | Test MAE | R² | Key innovation |
|---|---|---|---|---|
| v2 | XGBoost baseline | 23.1h | 0.08 | 10 features |
| v3 | +eta_naive +hist SOG +one-hot | 15.4h | -0.12 | Ratio feature, ship_type OH |
| v4 | +log-transform +sample weights | 11.1h | -0.12 | Log-target, LightGBM |
| v5 oracle | Per-horizon (perfect routing) | 3.5h | 0.90 | Specialized models per bin |
| v5 practical | LGBM 5-way router | 11.0h | - | Router bottleneck (53.6% acc) |
| v6 cascade | Binary cascade router | 26.8h | - | Cascade errors compound |
| **v6 global** | **Single model, all features** | **10.4h** | **-0.03** | **No routing needed** |
| v6 oracle | Per-horizon v6 features | 3.3h | 0.91 | Best possible with current data |
| **v7 LGBM** | **Single-stage v7 features** | **12.7h** | **0.13** | **MMSI split (harder eval)** |
| **v7 2-stage** | **Two-stage residual LGBM** | **10.3h** | **0.58** | **R² jump: -0.03→0.58** |
| **v7 deep** | **Two-Stage Full, 5000 trees, deep reg** | **10.0h** | **0.52** | **Breaks 10h barrier** |
| **v7 adaptive** | **Adaptive 2-stage (TTA<3h→no B)** | **10.0h** | **0.60** | **Soft blending** |
| v7 hard | Stage B on hard samples only | 10.4h | 0.54 | Better long horizons, worse short |
| **v8 2-stage** | **Native cat + HW loss + interactions** | **10.0h** | **0.53** | **Cleaner architecture, same perf** |
| **v8 single** | **Single-stage v8 (53 features)** | **12.8h** | **0.10** | **Native cat needs more trees** |

**Current best: v7 deep / v8 two-stage at 10.0h MAE** (↓ from 14.0h v6 with same MMSI split = −29%).
**Key insight: Two-stage model recovers oracle routing implicitly — no classifier needed.**
**Critical finding: v6's 10.4h had data leakage. MMSI split: 14.0h. Real improvement v7/v8: −29%.**
**⚠️ Plateau atteint: l'approche snapshot plafonne à ~10h. L'oracle (3.3h) prouve que l'info existe, mais inaccessible sans connaître l'horizon.**

## ⚠️ v8 — Le mur de l'information snapshot (2026-06-07)

### Ce qui a été testé
- **LightGBM natif catégoriel** (ship_type, nav_status) : 53 features au lieu de 85 one-hot → Single-stage moins bon (12.8h vs 12.0h), Two-stage équivalent
- **Horizon-weighted loss** : poids ×4 sur 3-8d, ×0.3 sur 0-1h → gain marginal (−0.03h)
- **Interactions** (dist×closing, mmsi_tta_bias, sog_vs_hist, weighted_efficiency) → incluses mais pas game-changing
- **Anchoring detection** (stationnaire + 10-80km du port) → signal utile mais <5% des samples
- **Feature pruning** (eta_phys redondants supprimés) → sans impact

### Pourquoi ça plafonne
À 500km, un navire à 10kn peut être à 20h ou 50h de l'arrivée. Cette ambiguïté est **fondamentale** :
- La distance haversine ignore les détroits, caps, zones d'attente
- Le SOG actuel n'est pas prédictif du SOG moyen sur 5 jours
- Les patterns d'approche portuaire (ancrage, slow steaming, fenêtres de terminal) sont invisibles dans un snapshot

**Le gap oracle (3.3h) vs modèle réel (10.0h) = 6.7h est la borne supérieure de ce qu'on peut gagner avec de meilleures features sur le même paradigme.**

### Prochains vrais leviers (au-delà du snapshot)
| Levier | Gain estimé | Complexité | Principe |
|---|---|---|---|
| **Séquence AIS brute** (LSTM/Transformer) | −2 à −5h | Élevée | 50 dernières positions → pattern de décélération, zigzag, attente |
| **Plus de données** | −1 à −3h | Dépend | 12 jours = peu de trajets complets. Élargir la fenêtre temporelle |
| **Routes empiriques** (paires origine→destination) | −1 à −2h | Moyenne | Depuis 71K arrivées, temps de trajet médian par corridor |
| **Données externes** (météo, courants, port schedules) | −1 à −3h | Très élevée | API météo, accès schedules portuaires |

### Ce qui ne sert à rien de continuer
- Plus d'arbres (5000 → 10000 : rendement décroissant prouvé)
- Plus de features snapshot (on a déjà 50+ features, le signal marginal est nul)
- Autres modèles GBDT (CatBoost, XGBoost : tous moins bons que LightGBM)
- DART, stacking, log-transform tricks (micro-optimisations sans impact structurel)
- searoute (librairie de visualisation, pas de routing réel)

### Leçons de v7+v8 pour la suite
1. Le **split MMSI est obligatoire** — sans ça, l'évaluation est gonflée de ~3.7h
2. Le **two-stage est la bonne architecture** — R² 0.53 vs −0.03, sans classifier fragile
3. Les **features MMSI et port** sont les plus utiles des nouvelles (mmsi_avg_tta #2, port_avg_tta top 10)
4. Les **features de trajectoire 6h** aident modestement (cog_std_6h, sog_range_6h en milieu de classement)
5. Le **deep training paie** : 2000 → 5000 arbres + régularisation forte = −0.3h constant

## v7 — 2026-06-07
### New features added (22 new numeric + better physics)
- **Trajectory 6h**: stop_fraction, slow_fraction, cog_std, heading_std, sog_range, sog_accel, turn_rate
- **Port-level**: avg_tta, sample_count, avg_sog, arrival_rate_per_hour
- **MMSI-level**: sog_std, sample_count, avg_tta, median_sog, sog_cv
- **Draught**: from ShipStaticData (100% coverage, 93.5% join rate)
- **Physics baselines**: eta_phys_6h, eta_phys_corrected, eta_phys_closing
- **Cyclical time**: hour_sin/cos, dow_sin/cos
- **Total**: 43 numeric + 42 one-hot = 85 features (vs 65 v6)

### Per-horizon breakdown (v7 two-stage, 2000 trees, MMSI-grouped split)
| Horizon | v6 MAE | v7 Two-Stage MAE | Change |
|---|---|---|---|
| 0-1h | 0.6h | 2.4h | ❌ Worse |
| 1-6h | 1.0h | 3.9h | ❌ Worse |
| 6-24h | 7.6h | 9.9h | ≈ Slightly worse |
| 1-3d | 38.8h | 22.8h | ✅ 41% better |
| 3-8d | 110.4h | 57.1h | ✅ 48% better |

### Key findings
1. **Two-stage architecture works**: R² 0.58 vs v6's -0.03 — massive improvement in explained variance
2. **Long horizons dramatically improved**: 3-8d from 110h → 57h, 1-3d from 39h → 23h
3. **Short horizons degraded**: 0-1h from 0.6h → 2.4h — residual correction adds noise to good predictions
4. **Single-stage models worse with v7 features**: 12.7h vs 10.4h v6 — feature bloat or harder split?
5. **CatBoost underperforms**: 14.2h despite native categorical support — LightGBM still king
6. **MMSI-grouped split is harder**: prevents same-vessel leakage, more realistic eval
7. **Port/MMSI features are top-ranked**: mmsi_avg_tta #2, mmsi_sample_count #8, port_avg_sog in top 15
8. **Trajectory features have modest impact**: sog_range_6h, cog_std_6h rank 11-12, useful but not game-changing

### Feature importance (v7 single-stage, top 10 by gain)
1. approach_efficiency (8.4%)
2. mmsi_avg_tta (7.7%) — NEW, best new feature
3. closing_speed_kmh (7.1%)
4. eta_naive_h (6.3%)
5. sog_trend_1h (5.1%)
6. avg_sog_1h (4.3%)
7. dist_to_dest_km (4.0%)
8. mmsi_sample_count (3.8%) — NEW
9. heading_std_1h (2.8%)
10. eta_phys_6h (2.7%) — NEW

### Why single-stage v7 < v6?
**Verified**: MMSI-grouped split adds ~3.7h penalty vs temporal split. Original v6 eval had data leakage.
- v6 features + temporal split: 10.3h MAE (matches reported 10.4h)
- v6 features + MMSI split: **14.0h** MAE (→ proper baseline)
- v7 features + MMSI split: 12.7h MAE (→ actually 9% better than v6 on same split)
- v7 adaptive two-stage: **10.0h** MAE (→ 29% better than v6 on same split)
→ **v7 features ARE an improvement; v6 evaluation was inflated by same-vessel leakage.**

### Adaptive two-stage details
- **Threshold**: 3h — if Stage A predicts TTA < 3h, skip Stage B (Stage A already accurate)
- **Soft blending**: sigmoid transition around threshold instead of hard cutoff
- **Effect**: Recovers short-horizon accuracy (2.4h → 1.4h at 0-1h, 3.9h → 3.2h at 1-6h)
- **Overall**: 10.3h → 10.0h (−3%)

### Next steps (ordered by expected impact)
1. **More trees** — models haven't converged at 2000; validation RMSE still dropping
2. **Train Stage B only on difficult samples** — make it specialize, not add noise to easy cases
3. **Quantile regression** — for uncertainty estimates (P10/P90 intervals)
4. **Route distance** — actual shipping lane distance instead of haversine (hard, needs routing API)
5. **Feature selection** — keep top 40 features (drop noisy eta_phys variants)
6. **Per-horizon weighted loss** — train Stage B with horizon-specific sample weights

## v6 — 2026-06-07
### New features added
- **navigational_status** (one-hot): 0=underway, 1=anchor, 5=moored, etc. 8 categories + other
- **heading_offset_deg**: |COG - true_heading| — vessel sideslip/drift signal
- **heading_std_1h**: variance of true_heading over last hour — course stability
- **avg_heading_1h**: average heading over last hour
- **rate_of_turn**: turning intensity (-128 = AIS "no data" → cleaned to 0)
- **rot_available**: binary flag for rate_of_turn availability
- **closing_speed_kmh**: (prev_dist - curr_dist) / time_gap from consecutive samples
- **approach_efficiency**: closing_speed / SOG — 0=sideways, 1=straight to port
- Total: 23 numeric + 32 ship_type OH + 9 nav_status OH + 1 other = **65 features**

### Feature importance (top 10, by gain)
1. eta_naive_h (12.3%) — ratio feature still #1
2. approach_efficiency (10.1%) — NEW, best trajectory signal
3. closing_speed_kmh (8.8%) — NEW, rate of approach
4. dist_to_dest_km (8.2%)
5. sog_trend_1h (6.8%)
6. avg_sog_1h (5.4%)
7. heading_std_1h (4.6%) — NEW, course stability
8. bearing_offset_deg (4.1%)
9. sog_vs_mmsi_avg (3.6%)
10. avg_sog_6h (3.5%)

### Per-horizon breakdown (v6 global model)
- 0-1h: MAE 0.6h
- 1-6h: MAE 1.0h
- 6-24h: MAE 7.6h
- 1-3d: MAE 38.8h
- 3-8d: MAE 110.4h

### Analysis
- Model excellent at <6h (sub-1h MAE) — production-ready for near-arrival predictions
- Medium horizons (6-24h) decent at 7.6h
- Long horizons still poor (38-110h) — fundamental limit of single-snapshot prediction
- approach_efficiency and closing_speed are the best new features — trajectory context matters
- Navigational status adds modest value (one-hot features low in importance)
- heading_std_1h is a surprisingly strong signal — course stability correlates with arrival intent

## What doesn't work
1. **Flat 5-way router**: LGBM classifier at 53.6% accuracy → MAE 11.0h (no improvement over global)
2. **Cascade binary routers**: Binary errors compound → MAE 26.8h (much worse)
3. **Soft routing**: Weighted ensemble of all models → MAE 14.5h (worse than global)
4. **eta_naive as standalone router**: Only 21% exact bin match → useless

## Why routing fails
- The fundamental problem: distinguishing 6-24h from 1-3d from 3-8d is inherently hard from snapshot features
- A vessel at 500km going 10kn looks identical whether it's 20h or 50h from arrival (depends on route, stops, currents)
- eta_naive = dist/sog is too noisy (SOG fluctuates, dist is straight-line not route)
- Per-horizon models prove it's possible (oracle 3.3h) but no practical router achieves this

## Files
```
ml/
├── detect_arrivals.py      # Arrival detection (2-pass)
├── fetch_ports.py          # UN/LOCODE download
├── utils.py                # Haversine, DuckDB catalog, destination cleaning
├── build_dataset.py        # Feature engineering (v6)
├── build_features_v7.py    # v7 features: trajectory, port, MMSI, draught, physics
├── train.py                # XGBoost + LightGBM training (v4)
├── train_horizon.py        # Per-horizon models + flat router (v5)
├── train_cascade.py        # Cascade binary router (failed)
├── train_v6_global.py      # Global model v6
├── train_v7.py             # Two-stage + adaptive + model comparison
├── tune.py                 # Optuna hyperparameter tuning
├── inference.py            # Inference module
├── PROGRESS.md             # This file
├── plan.md                 # Original plan
└── data/
    ├── arrivals.parquet              # 71K arrivals
    ├── positions_filtered.parquet    # 97M positions
    ├── dataset.parquet               # 198K training samples (v6, 32 cols)
    ├── dataset_v7.parquet            # 197K training samples (v7, 67 cols)
    ├── ports.parquet                 # 93K UN/LOCODE ports
    ├── models/                       # v5 models + router
    ├── models_cascade/               # v6 cascade models
    └── models_v7/                    # v7 two-stage (stage_a.txt, stage_b.txt)
```

## Remaining ideas (ordered by plausible impact)
1. ✅ ~~Port waiting time features~~ — Done in v7 (port_avg_tta, port_arrival_rate)
2. **Quantile regression** — LightGBM objective='quantile' for P10/P90 intervals
3. ✅ ~~Trajectory shape~~ — Done in v7 (stop_fraction, cog_std, sog_range, turn_rate over 6h)
4. **Hybrid physical model** — great-circle route distance instead of haversine (hard, needs routing API)
5. ✅ ~~MMSI-level features~~ — Done in v7 (mmsi_sog_std, mmsi_avg_tta, mmsi_sog_cv, mmsi_median_sog)
6. **Weather data** — wind, waves, currents (external API, significant complexity)
7. **Time-to-destination from AIS messages** — some ShipStaticData may have more precise destination info
8. **More trees** — 2000 not enough, validation still improving
9. **Train Stage B only on hard cases** — specialize the residual model
10. ✅ ~~Draught/tirant d'eau~~ — Collected (93.5% coverage), model uses it modestly
