"""Train cascade binary routers + per-horizon regression models.

Cascade architecture (easier binary decisions → better routing):
  Step 1: TTA < 6h?   → YES: discriminate 0-1h vs 1-6h → predict
                        → NO:  go to step 2
  Step 2: TTA < 24h?  → YES: predict with 6-24h model
                        → NO:  go to step 3
  Step 3: TTA < 72h?  → YES: predict with 1-3d model
                        → NO:  predict with 3-8d model

At inference, follow the same cascade path.
"""

import json
import pickle
import polars as pl
import numpy as np
from pathlib import Path
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.ensemble import RandomForestClassifier
import lightgbm as lgb
import xgboost as xgb
from utils import DATA_DIR

DATASET = DATA_DIR / "dataset.parquet"
MODEL_DIR = DATA_DIR / "models_cascade"
MODEL_DIR.mkdir(exist_ok=True)

# New v6 features
NUMERIC_FEATURES = [
    "dist_to_dest_km", "sog", "cog", "bearing_offset_deg",
    "heading_offset_deg", "rate_of_turn", "rot_available",
    "vessel_length", "vessel_width", "length_width_ratio",
    "hour_of_day", "day_of_week",
    "avg_sog_1h", "avg_sog_6h", "avg_sog_24h", "sog_trend_1h",
    "mmsi_avg_sog", "sog_vs_mmsi_avg",
    "heading_std_1h", "avg_heading_1h",
    "closing_speed_kmh", "approach_efficiency",
    "eta_naive_h",
]

TARGET = "time_to_arrival_hours"
SHIP_TYPE_MIN_COUNT = 500

# Log-transform for regression
LOG_TARGET = True

# Per-horizon bins (for regression models)
HORIZON_BINS = [
    (0, 1, "0-1h"),
    (1, 6, "1-6h"),
    (6, 24, "6-24h"),
    (24, 72, "1-3d"),
    (72, 200, "3-8d"),
]

# Cascade decision nodes: (threshold_hours, "description")
CASCADE = [
    (6, "under_6h"),
    (24, "under_24h"),
    (72, "under_72h"),
]


def load_data():
    df = pl.read_parquet(DATASET).sort(["mmsi", "pos_ts"])
    all_numeric = NUMERIC_FEATURES + [TARGET]
    df = df.select(all_numeric + ["ship_type", "nav_status"]).drop_nulls(subset=all_numeric)

    # One-hot ship_type
    st = df["ship_type"].fill_null(-1).cast(pl.Int64)
    type_counts = st.value_counts().sort("count", descending=True)
    top_types = {int(r[0]) for r in type_counts.iter_rows() if r[1] >= SHIP_TYPE_MIN_COUNT}

    st_np = st.to_numpy()
    onehot = {}
    for t in sorted(top_types):
        onehot[f"st_{t}"] = (st_np == t).astype(np.float32)
    other = ~np.isin(st_np, list(top_types))
    if other.any():
        onehot["st_other"] = other.astype(np.float32)

    # One-hot navigational status (only most common ones)
    nav = df["nav_status"].fill_null(-1).cast(pl.Int32).to_numpy()
    nav_counts = pl.Series(nav).value_counts().sort("count", descending=True)
    top_nav = {int(r[0]) for r in nav_counts.iter_rows() if r[1] >= 1000 and r[0] >= 0}
    for ns in sorted(top_nav):
        onehot[f"nav_{ns}"] = (nav == ns).astype(np.float32)
    nav_other = ~np.isin(nav, list(top_nav)) | (nav < 0)
    if nav_other.any():
        onehot["nav_other"] = nav_other.astype(np.float32)

    print(f"  One-hot: {len(top_types)} ship types + {len(top_nav)} nav status + other")

    X_num = df.select(NUMERIC_FEATURES).to_numpy().astype(np.float32)
    feature_names = list(NUMERIC_FEATURES) + sorted(onehot.keys())
    X = np.column_stack([X_num] + [onehot[c] for c in sorted(onehot.keys())])
    y = df.select(TARGET).to_numpy().ravel().astype(np.float32)

    return X, y, feature_names


def train_binary_classifier(X_train, y_train, threshold_hours, name):
    """Train binary classifier: TTA < threshold?"""
    y_bin = (y_train < threshold_hours).astype(np.int32)
    pos = y_bin.sum()
    neg = len(y_bin) - pos
    scale_pos_weight = neg / max(pos, 1)

    model = xgb.XGBClassifier(
        n_estimators=100, max_depth=5, learning_rate=0.1,
        scale_pos_weight=min(scale_pos_weight, 10.0),
        random_state=42, verbosity=0, n_jobs=4,
    )
    model.fit(X_train, y_bin)
    acc = model.score(X_train, y_bin)
    print(f"  {name:12s} (<{threshold_hours:3d}h): n_pos={pos:6d} n_neg={neg:6d} "
          f"scale={scale_pos_weight:.1f} train_acc={acc:.3f}")
    return model


def train_regressor(X_train, y_train, X_test, y_test, name):
    """Train LightGBM regressor on log-target for specific horizon bin."""
    y_train_log = np.log1p(y_train)
    y_test_log = np.log1p(y_test) if len(y_test) > 0 else None

    dtrain = lgb.Dataset(X_train, label=y_train_log)
    dtest = lgb.Dataset(X_test, label=y_test_log) if len(y_test) > 0 else None

    params = {
        "objective": "regression", "metric": "rmse", "boosting_type": "gbdt",
        "num_leaves": 88, "learning_rate": 0.0719,
        "feature_fraction": 0.79, "bagging_fraction": 0.79, "bagging_freq": 7,
        "min_data_in_leaf": max(5, 86 // 4 if len(y_train) < 50000 else 20),
        "lambda_l1": 0.037, "lambda_l2": 0.00064,
        "verbose": -1, "num_threads": 4, "seed": 42,
    }

    model = lgb.train(params, dtrain, num_boost_round=500,
                      valid_sets=[dtest] if dtest else None,
                      callbacks=[lgb.early_stopping(30), lgb.log_evaluation(0)])

    yp_train = np.expm1(model.predict(X_train))
    mae_train = mean_absolute_error(y_train, yp_train)
    within_2h = (np.abs(y_train - yp_train) <= 2).mean()

    if len(y_test) > 0:
        yp_test = np.expm1(model.predict(X_test))
        mae_test = mean_absolute_error(y_test, yp_test)
        within_6h = (np.abs(y_test - yp_test) <= 6).mean()
        print(f"  {name:6s}: train_MAE={mae_train:.1f}h test_MAE={mae_test:.1f}h "
              f"±6h={within_6h:.0%} n_train={len(y_train)} n_test={len(y_test)}")
    else:
        print(f"  {name:6s}: train_MAE={mae_train:.1f}h ±2h={within_2h:.0%} n={len(y_train)}")

    return model


def predict_cascade(X, classifiers, regressors):
    """Predict TTA using cascade routing."""
    n = len(X)

    # Step 1: < 6h?
    is_under_6h = classifiers["under_6h"].predict(X) == 1
    # Step 2: < 24h? (only for those NOT under 6h)
    not_under_6h = ~is_under_6h
    is_under_24h = np.zeros(n, dtype=bool)
    if not_under_6h.any():
        is_under_24h[not_under_6h] = classifiers["under_24h"].predict(X[not_under_6h]) == 1
    # Step 3: < 72h? (only for those NOT under 24h)
    not_under_24h = not_under_6h & ~is_under_24h
    is_under_72h = np.zeros(n, dtype=bool)
    if not_under_24h.any():
        is_under_72h[not_under_24h] = classifiers["under_72h"].predict(X[not_under_24h]) == 1

    # Within 6h: discriminate 0-1h vs 1-6h
    predictions = np.zeros(n)
    under_6h_mask = is_under_6h
    if under_6h_mask.any():
        # Use simpler heuristic: if dist/sog < 1h → 0-1h model, else 1-6h
        eta_naive_idx = NUMERIC_FEATURES.index("eta_naive_h")
        eta_naive = X[under_6h_mask, eta_naive_idx]
        is_under_1h = eta_naive < 1.0
        mask_0_1h = under_6h_mask.copy()
        mask_0_1h[under_6h_mask] = is_under_1h
        mask_1_6h = under_6h_mask.copy()
        mask_1_6h[under_6h_mask] = ~is_under_1h

        if mask_0_1h.any() and "0-1h" in regressors:
            predictions[mask_0_1h] = np.expm1(regressors["0-1h"].predict(X[mask_0_1h]))
        if mask_1_6h.any() and "1-6h" in regressors:
            predictions[mask_1_6h] = np.expm1(regressors["1-6h"].predict(X[mask_1_6h]))

    # 6-24h
    mask_6_24h = is_under_24h & ~is_under_6h
    if mask_6_24h.any() and "6-24h" in regressors:
        predictions[mask_6_24h] = np.expm1(regressors["6-24h"].predict(X[mask_6_24h]))

    # 1-3d
    mask_1_3d = is_under_72h & ~is_under_24h
    if mask_1_3d.any() and "1-3d" in regressors:
        predictions[mask_1_3d] = np.expm1(regressors["1-3d"].predict(X[mask_1_3d]))

    # 3-8d
    mask_3_8d = ~is_under_72h & ~is_under_24h & ~is_under_6h
    if mask_3_8d.any() and "3-8d" in regressors:
        predictions[mask_3_8d] = np.expm1(regressors["3-8d"].predict(X[mask_3_8d]))

    return predictions


def main():
    print("=" * 60)
    print("Cascade Router + Per-Horizon Models")
    print("=" * 60)

    X, y, feature_names = load_data()
    split = int(len(X) * 0.8)
    X_train, X_test = X[:split], X[split:]
    y_train, y_test = y[:split], y[split:]

    print(f"  Train: {len(y_train)}  Test: {len(y_test)}  Features: {X.shape[1]}")

    # ── Train cascade classifiers ──
    print(f"\n── Cascade binary classifiers ──")
    classifiers = {}
    for threshold, name in CASCADE:
        classifiers[name] = train_binary_classifier(X_train, y_train, threshold, name)

    # ── Train per-horizon regressors ──
    print(f"\n── Horizon regressors ──")
    regressors = {}
    for lo, hi, name in HORIZON_BINS:
        train_mask = (y_train >= lo) & (y_train < hi)
        test_mask = (y_test >= lo) & (y_test < hi)
        if train_mask.sum() >= 100:
            regressors[name] = train_regressor(
                X_train[train_mask], y_train[train_mask],
                X_test[test_mask], y_test[test_mask], name
            )

    # ── Evaluate routing accuracy ──
    print(f"\n── Routing evaluation ──")
    bins = [0, 1, 6, 24, 72, 200]
    y_test_bin = np.clip(np.digitize(y_test, bins) - 1, 0, len(HORIZON_BINS) - 1)

    # Cascade routing: determine predicted bin
    pred_bin = np.full(len(y_test), -1, dtype=int)
    is_under_6h = classifiers["under_6h"].predict(X_test) == 1
    eta_naive_idx = NUMERIC_FEATURES.index("eta_naive_h")
    eta_naive = X_test[:, eta_naive_idx]

    mask_0_1h = is_under_6h & (eta_naive < 1.0)
    mask_1_6h = is_under_6h & (eta_naive >= 1.0)
    pred_bin[mask_0_1h] = 0
    pred_bin[mask_1_6h] = 1

    not_6h = ~is_under_6h
    if not_6h.any():
        is_under_24h = np.zeros(len(y_test), dtype=bool)
        is_under_24h[not_6h] = classifiers["under_24h"].predict(X_test[not_6h]) == 1
        pred_bin[not_6h & is_under_24h] = 2

        not_24h = not_6h & ~is_under_24h
        if not_24h.any():
            is_under_72h = np.zeros(len(y_test), dtype=bool)
            is_under_72h[not_24h] = classifiers["under_72h"].predict(X_test[not_24h]) == 1
            pred_bin[not_24h & is_under_72h] = 3
            pred_bin[not_24h & ~is_under_72h] = 4

    valid = pred_bin >= 0
    acc = (y_test_bin[valid] == pred_bin[valid]).mean()
    adj = (np.abs(y_test_bin[valid] - pred_bin[valid]) <= 1).mean()
    print(f"  Cascade accuracy:  {acc:.1%}")
    print(f"  Cascade adjacent:  {adj:.1%}")

    for i, (lo, hi, name) in enumerate(HORIZON_BINS):
        mask = y_test_bin == i
        if mask.sum() > 0:
            recall = (pred_bin[mask] == i).mean()
            print(f"    {name:6s}: recall {recall:.1%} (n={mask.sum()})")

    # ── Ensemble prediction ──
    print(f"\n── Prediction results ──")

    # Oracle (true bin)
    y_pred_oracle = np.zeros_like(y_test)
    for i, (lo, hi, name) in enumerate(HORIZON_BINS):
        mask = (y_test >= lo) & (y_test < hi)
        if mask.sum() > 0 and name in regressors:
            y_pred_oracle[mask] = np.expm1(regressors[name].predict(X_test[mask]))

    # Cascade
    y_pred_cascade = predict_cascade(X_test, classifiers, regressors)

    for label, yp in [("Oracle (true bin)", y_pred_oracle), ("Cascade router", y_pred_cascade)]:
        valid = yp > 0
        yt, yp_v = y_test[valid], yp[valid]
        mae = mean_absolute_error(yt, yp_v)
        within_6 = (np.abs(yt - yp_v) <= 6).mean()
        within_24 = (np.abs(yt - yp_v) <= 24).mean()
        print(f"  {label:20s}: MAE={mae:.1f}h  ±6h={within_6:.0%}  ±24h={within_24:.0%}  n={valid.sum()}")

    # Naive
    naive = X_test[:, 0] / np.maximum(X_test[:, 1], 1.0)
    print(f"  {'Naive (dist/sog)':20s}: MAE={mean_absolute_error(y_test, naive):.1f}h")

    # ── Save models ──
    print(f"\n── Saving models to {MODEL_DIR} ──")
    import pickle
    for name, clf in classifiers.items():
        pickle.dump(clf, open(MODEL_DIR / f"classifier_{name}.pkl", "wb"))
    for name, model in regressors.items():
        model.save_model(str(MODEL_DIR / f"regressor_{name.replace('-', '_')}.txt"))

    meta = {
        "feature_names": feature_names,
        "numeric_features": NUMERIC_FEATURES,
        "horizon_bins": [(lo, hi, name) for lo, hi, name in HORIZON_BINS],
        "cascade": [(th, name) for th, name in CASCADE],
    }
    json.dump(meta, open(MODEL_DIR / "metadata.json", "w"), indent=2)
    print(f"  Saved {len(classifiers)} classifiers + {len(regressors)} regressors + metadata")

    print("\n✓ Done.")


if __name__ == "__main__":
    main()
