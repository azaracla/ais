"""Train specialized LightGBM models per time horizon + inference router.

Each bin gets its own model. A RandomForest classifier routes new samples
to the correct model at inference time.

Saves: models as .txt files, router as .pkl, metadata as .json.
"""

import json
import pickle
import polars as pl
import numpy as np
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.ensemble import RandomForestClassifier
import lightgbm as lgb
from utils import DATA_DIR

DATASET = DATA_DIR / "dataset.parquet"
MODEL_DIR = DATA_DIR / "models"
MODEL_DIR.mkdir(exist_ok=True)

NUMERIC_FEATURES = [
    "dist_to_dest_km", "sog", "cog", "bearing_offset_deg",
    "vessel_length", "vessel_width", "length_width_ratio",
    "hour_of_day", "day_of_week",
    "avg_sog_1h", "avg_sog_6h", "avg_sog_24h", "sog_trend_1h",
    "mmsi_avg_sog", "sog_vs_mmsi_avg", "eta_naive_h",
]

TARGET = "time_to_arrival_hours"

HORIZON_BINS = [
    (0, 1, "0-1h"),
    (1, 6, "1-6h"),
    (6, 24, "6-24h"),
    (24, 72, "1-3d"),
    (72, 200, "3-8d"),
]

SHIP_TYPE_MIN_COUNT = 500


def load_data():
    df = pl.read_parquet(DATASET).sort(["mmsi", "pos_ts"])
    all_numeric = NUMERIC_FEATURES + [TARGET]
    df = df.select(all_numeric + ["ship_type"]).drop_nulls(subset=all_numeric)

    ship_types = df["ship_type"].fill_null(-1).cast(pl.Int64)
    type_counts = ship_types.value_counts().sort("count", descending=True)
    top_types = {int(r[0]) for r in type_counts.iter_rows() if r[1] >= SHIP_TYPE_MIN_COUNT}

    st_np = ship_types.to_numpy()
    onehot_cols = {}
    for st in sorted(top_types):
        onehot_cols[f"st_{st}"] = (st_np == st).astype(np.float32)
    other = ~np.isin(st_np, list(top_types))
    if other.any():
        onehot_cols["st_other"] = other.astype(np.float32)

    X_num = df.select(NUMERIC_FEATURES).to_numpy().astype(np.float32)
    X = np.column_stack([X_num] + [onehot_cols[c] for c in sorted(onehot_cols.keys())])
    y = df.select(TARGET).to_numpy().ravel().astype(np.float32)
    fn = list(NUMERIC_FEATURES) + sorted(onehot_cols.keys())
    return X, y, fn, top_types


def get_lgb_params():
    best_path = DATA_DIR / "best_params.json"
    best = json.loads(best_path.read_text()) if best_path.exists() else {}
    return {
        "objective": "regression",
        "metric": "rmse",
        "boosting_type": "gbdt",
        "num_leaves": best.get("num_leaves", 88),
        "learning_rate": best.get("learning_rate", 0.0719),
        "feature_fraction": best.get("feature_fraction", 0.792),
        "bagging_fraction": best.get("bagging_fraction", 0.787),
        "bagging_freq": best.get("bagging_freq", 7),
        "min_data_in_leaf": max(5, best.get("min_data_in_leaf", 86) // 4),
        "lambda_l1": best.get("lambda_l1", 0.0372),
        "lambda_l2": best.get("lambda_l2", 0.000642),
        "verbose": -1,
        "num_threads": 4,
        "seed": 42,
    }


def train_router(X_train, y_train, X_test, y_test):
    """Train a LightGBM classifier to route samples to the correct horizon model."""
    bins = [lo for lo, _, _ in HORIZON_BINS] + [200]
    y_train_bin = np.clip(np.digitize(y_train, bins) - 1, 0, len(HORIZON_BINS) - 1)
    y_test_bin = np.clip(np.digitize(y_test, bins) - 1, 0, len(HORIZON_BINS) - 1)

    # Class weights to balance rare bins
    from collections import Counter
    counts = Counter(y_train_bin)
    n_samples = len(y_train_bin)
    n_classes = len(HORIZON_BINS)
    class_weight = {c: n_samples / (n_classes * counts.get(c, 1)) for c in range(n_classes)}

    dtrain = lgb.Dataset(X_train, label=y_train_bin)
    dtest = lgb.Dataset(X_test, label=y_test_bin, reference=dtrain)

    clf = lgb.train(
        {
            "objective": "multiclass",
            "num_class": n_classes,
            "metric": "multi_logloss",
            "boosting_type": "gbdt",
            "num_leaves": 63,
            "learning_rate": 0.05,
            "feature_fraction": 0.8,
            "bagging_fraction": 0.8,
            "min_data_in_leaf": 50,
            "verbose": -1,
            "num_threads": 4,
            "seed": 42,
        },
        dtrain,
        num_boost_round=200,
        valid_sets=[dtest],
        callbacks=[lgb.early_stopping(20), lgb.log_evaluation(0)],
    )

    pred_train = np.argmax(clf.predict(X_train), axis=1)
    pred_test = np.argmax(clf.predict(X_test), axis=1)
    proba_test = clf.predict(X_test)

    acc_train = (y_train_bin == pred_train).mean()
    acc_test = (y_test_bin == pred_test).mean()
    adj_test = (np.abs(y_test_bin - pred_test) <= 1).mean()

    print(f"\n── Router (LightGBM) ──")
    print(f"  Train accuracy:      {acc_train:.1%}")
    print(f"  Test accuracy:       {acc_test:.1%}")
    print(f"  Test adjacent (±1):  {adj_test:.1%}")

    for i, (lo, hi, name) in enumerate(HORIZON_BINS):
        mask = y_test_bin == i
        if mask.sum() > 0:
            acc = (pred_test[mask] == i).mean()
            print(f"    {name:6s}: recall {acc:.1%} (n={mask.sum()})")

    # Save router
    router_path = MODEL_DIR / "router.txt"
    clf.save_model(str(router_path))
    print(f"  Saved to {router_path}")

    return clf, pred_test, proba_test


def main():
    print("=" * 60)
    print("Per-Horizon Models + Inference Router")
    print("=" * 60)

    X, y, feature_names, top_types = load_data()
    split = int(len(X) * 0.8)
    X_train, X_test = X[:split], X[split:]
    y_train, y_test = y[:split], y[split:]
    print(f"  Train: {len(y_train)}  Test: {len(y_test)}  Features: {X.shape[1]}")

    # ── Train router ──
    router, router_preds, router_proba = train_router(X_train, y_train, X_test, y_test)

    # ── Train per-horizon models ──
    models = {}
    metrics = {}

    print(f"\n── Horizon models ──")
    for lo, hi, name in HORIZON_BINS:
        train_mask = (y_train >= lo) & (y_train < hi)
        test_mask = (y_test >= lo) & (y_test < hi)
        n_train = train_mask.sum()

        if n_train < 100:
            print(f"  {name}: SKIP (n={n_train})")
            continue

        yt_log = np.log1p(y_train[train_mask])
        dtrain = lgb.Dataset(X_train[train_mask], label=yt_log)

        model = lgb.train(
            get_lgb_params(), dtrain,
            num_boost_round=500,
            callbacks=[lgb.log_evaluation(0)],
        )
        models[name] = model

        # Save model
        model_path = MODEL_DIR / f"model_{name.replace('-', '_')}.txt"
        model.save_model(str(model_path))

        # Evaluate on test
        if test_mask.sum() > 0:
            yp = np.expm1(model.predict(X_test[test_mask]))
            mae = mean_absolute_error(y_test[test_mask], yp)
            within_6 = (np.abs(y_test[test_mask] - yp) <= 6).mean()
            metrics[name] = {"mae": mae, "within_6h": within_6, "n_train": n_train, "n_test": test_mask.sum()}
            print(f"  {name:6s}: n={n_train:6d}  test_MAE={mae:.1f}h  ±6h={within_6:.0%}")

    # ── Save metadata ──
    meta = {
        "horizon_bins": [(lo, hi, name) for lo, hi, name in HORIZON_BINS],
        "feature_names": feature_names,
        "numeric_features": NUMERIC_FEATURES,
        "ship_type_min_count": SHIP_TYPE_MIN_COUNT,
        "top_ship_types": sorted(top_types),
        "metrics": {k: {kk: float(vv) if isinstance(vv, (np.floating, np.integer)) else int(vv) for kk, vv in v.items()} for k, v in metrics.items()},
    }
    json.dump(meta, open(MODEL_DIR / "metadata.json", "w"), indent=2)
    print(f"\n  Saved {len(models)} models + metadata to {MODEL_DIR}")

    # ── Ensemble evaluation: oracle vs router ──
    print(f"\n── Ensemble evaluation ──")
    bins = [lo for lo, _, _ in HORIZON_BINS] + [200]

    # Oracle routing (true bin)
    y_pred_oracle = np.zeros_like(y_test)
    for i, (lo, hi, name) in enumerate(HORIZON_BINS):
        mask = (y_test >= lo) & (y_test < hi)
        if mask.sum() > 0 and name in models:
            y_pred_oracle[mask] = np.expm1(models[name].predict(X_test[mask]))

    # Router-based prediction
    y_pred_router = np.zeros_like(y_test)
    for i, (lo, hi, name) in enumerate(HORIZON_BINS):
        mask = router_preds == i
        if mask.sum() > 0 and name in models:
            y_pred_router[mask] = np.expm1(models[name].predict(X_test[mask]))

    # Soft routing: weighted average of all models based on classifier prob
    # router_proba already set above from LGBM classifier
    y_pred_soft = np.zeros_like(y_test)
    for i, (lo, hi, name) in enumerate(HORIZON_BINS):
        if name in models:
            y_pred_soft += router_proba[:, i] * np.expm1(models[name].predict(X_test))

    print(f"\n  Routing strategy comparison:")
    for strategy, yp in [("Oracle (true bin)", y_pred_oracle),
                          ("Hard router", y_pred_router),
                          ("Soft (weighted)", y_pred_soft)]:
        mae = mean_absolute_error(y_test, yp)
        within_6 = (np.abs(y_test - yp) <= 6).mean()
        within_24 = (np.abs(y_test - yp) <= 24).mean()
        print(f"  {strategy:20s}: MAE={mae:.1f}h  ±6h={within_6:.0%}  ±24h={within_24:.0%}")

    # Naive
    naive = X_test[:, 0] / np.maximum(X_test[:, 1], 1.0)
    naive_mae = mean_absolute_error(y_test, naive)
    print(f"  {'Naive (dist/sog)':20s}: MAE={naive_mae:.1f}h")

    print("\n✓ Done. Models ready for inference.")


if __name__ == "__main__":
    main()
