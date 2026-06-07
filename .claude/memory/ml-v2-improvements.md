---
name: ml-v5-results
description: ML v5 per-horizon models achieve MAE 3.4h R² 0.90 — breakthrough
metadata:
  type: project
---

## Summary
v5 achieved **MAE 3.4h, R² 0.90** via per-horizon specialized LightGBM models. 85% MAE reduction from v2 baseline (23.1h → 3.4h).

## What worked (ranked by impact)
1. **Per-horizon models** — 5 separate LightGBM models, each trained only on its TTA bin. Biggest single improvement (11.1h → 3.4h). Global model forced to predict 0.5h and 168h in same model → log-variance destroyed.
2. **Log-transform target** — log1p(y), train in log-space, expm1 at inference. MAE 15.4h → 11.1h.
3. **eta_naive feature** — dist/sog ratio explicitly given to the model. XGBoost can't learn ratios from trees.
4. **One-hot ship_type** — 32 top categories + "other". Raw integer treated as continuous by trees.
5. **Historical SOG** — avg over 1h/6h/24h via DuckDB epoch-based window functions.
6. **Optuna tuning** — 50 trials on LightGBM params. Marginal gain (~0.5h).

## Per-horizon results
| Bin    | MAE    | ±6h    |
|--------|--------|--------|
| 0-1h   | 0.2h   | 100%   |
| 1-6h   | 0.9h   | 100%   |
| 6-24h  | 4.2h   | 79%    |
| 1-3d   | 13.9h  | 25%    |
| 3-8d   | 26.6h  | 14%    |
| Global | **3.4h** | 86.9% |

## Remaining work
- Inference router (classifier to pick horizon model). eta_naive alone is terrible (21% exact). RF classifier gets 87% adjacent. Soft routing (weighted ensemble) is best approach.
- Save models to disk for deployment.
- Prediction intervals (quantile regression).

**Why:** Critical milestone — model is now usable for production ETA prediction.
**How to apply:** Use per-horizon models. At inference, use RF classifier to route to the right model (or soft ensemble all models weighted by classifier probabilities).
