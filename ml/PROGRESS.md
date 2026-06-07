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

**Current best practical model: v6 global at 10.4h MAE** (down from 23.1h v2 = 55% reduction).

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
├── detect_arrivals.py    # Arrival detection (2-pass)
├── fetch_ports.py        # UN/LOCODE download
├── utils.py              # Haversine, DuckDB catalog, destination cleaning
├── build_dataset.py      # Feature engineering (v6)
├── train.py              # XGBoost + LightGBM training (v4)
├── train_horizon.py      # Per-horizon models + flat router (v5)
├── train_cascade.py      # Cascade binary router (failed)
├── train_v6_global.py    # Global model v6 (current best)
├── tune.py               # Optuna hyperparameter tuning
├── inference.py          # Inference module
├── PROGRESS.md           # This file
├── plan.md               # Original plan
└── data/
    ├── arrivals.parquet           # 71K arrivals
    ├── positions_filtered.parquet  # 97M positions (now with nav_status, rate_of_turn)
    ├── dataset.parquet            # 198K training samples (v6)
    ├── ports.parquet              # 93K UN/LOCODE ports
    ├── models/                    # v5 models + router
    └── models_cascade/            # v6 cascade models
```

## Remaining ideas (ordered by plausible impact)
1. **Port waiting time features** — average time between arrival detection and "moored" status per port
2. **Quantile regression** — LightGBM objective='quantile' for P10/P90 intervals
3. **Trajectory shape** — more than 2 points (e.g., last 3 samples → curvature, acceleration)
4. **Hybrid physical model** — great-circle route distance instead of haversine, ETA = route_dist / avg_speed + port_wait
5. **MMSI-level features** — per-vessel historical speed distribution, typical routes
6. **Weather data** — wind, waves, currents (external API, significant complexity)
7. **Time-to-destination from AIS messages** — some ShipStaticData may have more precise destination info
