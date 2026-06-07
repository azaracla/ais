"""Train specialized LightGBM models per time horizon bin.

Each bin gets its own model trained only on samples in that range.
Ensemble prediction: pick the right model based on initial eta_naive estimate.
"""

import json
import polars as pl
import numpy as np
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
import lightgbm as lgb
from utils import DATA_DIR

DATASET = DATA_DIR / "dataset.parquet"

NUMERIC_FEATURES = [
    "dist_to_dest_km", "sog", "cog", "bearing_offset_deg",
    "vessel_length", "vessel_width", "length_width_ratio",
    "hour_of_day", "day_of_week",
    "avg_sog_1h", "avg_sog_6h", "avg_sog_24h", "sog_trend_1h",
    "mmsi_avg_sog", "sog_vs_mmsi_avg", "eta_naive_h",
]

TARGET = "time_to_arrival_hours"
LOG_TARGET = True

HORIZON_BINS = [
    (0, 1, "0-1h"),
    (1, 6, "1-6h"),
    (6, 24, "6-24h"),
    (24, 72, "1-3d"),
    (72, 200, "3-8d"),
]


def load_data():
    df = pl.read_parquet(DATASET).sort(["mmsi", "pos_ts"])
    all_numeric = NUMERIC_FEATURES + [TARGET]
    df = df.select(all_numeric + ["ship_type"]).drop_nulls(subset=all_numeric)

    ship_types = df["ship_type"].fill_null(-1).cast(pl.Int64)
    type_counts = ship_types.value_counts().sort("count", descending=True)
    top_types = {int(r[0]) for r in type_counts.iter_rows() if r[1] >= 500}

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
    return X, y, fn


def get_lgb_params():
    best_path = DATA_DIR / "best_params.json"
    if best_path.exists():
        best = json.loads(best_path.read_text())
    else:
        best = {}
    return {
        "objective": "regression",
        "metric": "rmse",
        "boosting_type": "gbdt",
        "num_leaves": best.get("num_leaves", 88),
        "learning_rate": best.get("learning_rate", 0.0719),
        "feature_fraction": best.get("feature_fraction", 0.792),
        "bagging_fraction": best.get("bagging_fraction", 0.787),
        "bagging_freq": best.get("bagging_freq", 7),
        "min_data_in_leaf": max(10, best.get("min_data_in_leaf", 86) // 2),
        "lambda_l1": best.get("lambda_l1", 0.037),
        "lambda_l2": best.get("lambda_l2", 0.00064),
        "verbose": -1,
        "num_threads": 4,
        "seed": 42,
    }


def main():
    print("=" * 60)
    print("Per-Horizon Specialized Models")
    print("=" * 60)

    X, y, feature_names = load_data()
    split = int(len(X) * 0.8)
    X_train, X_test = X[:split], X[split:]
    y_train, y_test = y[:split], y[split:]
    print(f"  Train: {len(y_train)}  Test: {len(y_test)}")

    # ── Train one model per horizon bin ──
    models = {}
    y_pred_test = np.zeros_like(y_test)
    y_pred_train = np.zeros_like(y_train)

    print(f"\n── Per-bin training ──")
    for lo, hi, name in HORIZON_BINS:
        train_mask = (y_train >= lo) & (y_train < hi)
        test_mask = (y_test >= lo) & (y_test < hi)
        n_train = train_mask.sum()
        n_test = test_mask.sum()

        if n_train < 100:
            print(f"  {name}: SKIP (n_train={n_train} < 100)")
            continue

        print(f"  {name}: n_train={n_train:6d}  n_test={n_test:5d}  ", end="", flush=True)

        yt_log = np.log1p(y_train[train_mask])
        ye_log = np.log1p(y_test[test_mask]) if n_test > 0 else None

        dtrain = lgb.Dataset(X_train[train_mask], label=yt_log)
        dtest = lgb.Dataset(X_test[test_mask], label=ye_log) if n_test > 0 else None

        model = lgb.train(
            get_lgb_params(), dtrain,
            num_boost_round=500,
            valid_sets=[dtest] if dtest else None,
            callbacks=[lgb.early_stopping(30), lgb.log_evaluation(0)],
        )

        models[name] = model

        # Predict
        yp_train = np.expm1(model.predict(X_train[train_mask]))
        y_pred_train[train_mask] = yp_train
        mae_train = mean_absolute_error(y_train[train_mask], yp_train)

        if n_test > 0:
            yp_test = np.expm1(model.predict(X_test[test_mask]))
            y_pred_test[test_mask] = yp_test
            mae_test = mean_absolute_error(y_test[test_mask], yp_test)
            within_2h = (np.abs(y_test[test_mask] - yp_test) <= 2).mean()
            within_6h = (np.abs(y_test[test_mask] - yp_test) <= 6).mean()
            print(f"MAE={mae_test:.1f}h  ±2h={within_2h:.0%}  ±6h={within_6h:.0%}")
        else:
            print(f"MAE_train={mae_train:.1f}h")

    # ── Combined metrics ──
    valid = y_pred_test > 0
    yt, yp = y_test[valid], y_pred_test[valid]
    n_valid = valid.sum()

    print(f"\n── Ensemble results ({n_valid}/{len(y_test)} samples covered) ──")
    mae = mean_absolute_error(yt, yp)
    rmse = np.sqrt(mean_squared_error(yt, yp))
    r2 = r2_score(yt, yp)
    print(f"  MAE:   {mae:.1f}h")
    print(f"  RMSE:  {rmse:.1f}h")
    print(f"  R²:    {r2:.4f}")

    for t in [1, 3, 6, 12, 24]:
        acc = (np.abs(yt - yp) <= t).mean()
        print(f"  within {t:2d}h: {acc:.1%}")

    # Naive
    sog = np.maximum(X_test[valid, 1], 1.0)
    naive = X_test[valid, 0] / sog
    naive_mae = mean_absolute_error(yt, naive)
    print(f"  Naive MAE (dist/sog): {naive_mae:.1f}h")
    print(f"  Improvement:          {(1 - mae / naive_mae) * 100:.1f}%")

    # ── Per-bin breakdown ──
    print(f"\n── Per-horizon breakdown ──")
    for lo, hi, name in HORIZON_BINS:
        mask = (y_test >= lo) & (y_test < hi)
        if mask.sum() > 0 and y_pred_test[mask].sum() > 0:
            err = np.abs(y_test[mask] - y_pred_test[mask])
            print(f"  {name:6s}: MAE={err.mean():.1f}h  n={mask.sum()}")

    print("\n✓ Done.")


if __name__ == "__main__":
    main()
