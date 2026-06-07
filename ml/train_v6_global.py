"""Train global LightGBM v6 — single model, no routing.

All v6 features: nav_status, heading, rate_of_turn, closing_speed, etc.
Log-transform target, sample weighting.
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
LOG_TARGET = True


def load_data():
    df = pl.read_parquet(DATASET).sort(["mmsi", "pos_ts"])
    all_numeric = NUMERIC_FEATURES + [TARGET]
    df = df.select(all_numeric + ["ship_type", "nav_status"]).drop_nulls(subset=all_numeric)

    # One-hot ship_type
    st = df["ship_type"].fill_null(-1).cast(pl.Int64)
    type_counts = st.value_counts().sort("count", descending=True)
    top_types = {int(r[0]) for r in type_counts.iter_rows() if r[1] >= 500}
    st_np = st.to_numpy()
    onehot = {}
    for t in sorted(top_types):
        onehot[f"st_{t}"] = (st_np == t).astype(np.float32)
    other = ~np.isin(st_np, list(top_types))
    if other.any():
        onehot["st_other"] = other.astype(np.float32)

    # One-hot navigational_status
    nav = df["nav_status"].fill_null(-1).cast(pl.Int32).to_numpy()
    nav_counts = pl.Series(nav).value_counts().sort("count", descending=True)
    top_nav = {int(r[0]) for r in nav_counts.iter_rows() if r[1] >= 1000 and r[0] >= 0}
    for ns in sorted(top_nav):
        onehot[f"nav_{ns}"] = (nav == ns).astype(np.float32)
    nav_other = ~np.isin(nav, list(top_nav)) | (nav < 0)
    if nav_other.any():
        onehot["nav_other"] = nav_other.astype(np.float32)

    print(f"  One-hot: {len(top_types)} ship + {len(top_nav)} nav + other")

    X_num = df.select(NUMERIC_FEATURES).to_numpy().astype(np.float32)
    feature_names = list(NUMERIC_FEATURES) + sorted(onehot.keys())
    X = np.column_stack([X_num] + [onehot[c] for c in sorted(onehot.keys())])
    y = df.select(TARGET).to_numpy().ravel().astype(np.float32)

    return X, y, feature_names


def evaluate(y_true, y_pred, X_test=None, label="Test"):
    mae = mean_absolute_error(y_true, y_pred)
    rmse = np.sqrt(mean_squared_error(y_true, y_pred))
    r2 = r2_score(y_true, y_pred)

    for t in [1, 3, 6, 12, 24]:
        acc = (np.abs(y_true - y_pred) <= t).mean()
        print(f"  within {t:2d}h: {acc:.1%}")

    print(f"  MAE:   {mae:.1f}h")
    print(f"  RMSE:  {rmse:.1f}h")
    print(f"  R²:    {r2:.4f}")

    if X_test is not None:
        naive = X_test[:, 0] / np.maximum(X_test[:, 1], 1.0)
        print(f"  Naive MAE (dist/sog): {mean_absolute_error(y_true, naive):.1f}h")
        print(f"  Improvement:          {(1 - mae / mean_absolute_error(y_true, naive)) * 100:.1f}%")


def main():
    print("=" * 60)
    print("Global LightGBM v6 — All Features")
    print(f"  Log-target: {LOG_TARGET}")
    print("=" * 60)

    X, y, feature_names = load_data()
    split = int(len(X) * 0.8)
    X_train, X_test = X[:split], X[split:]
    y_train, y_test = y[:split], y[split:]
    y_train_orig, y_test_orig = y_train.copy(), y_test.copy()

    if LOG_TARGET:
        y_train = np.log1p(y_train)
        y_test = np.log1p(y_test)

    # Sample weights: inverse TTA
    w_train = 1.0 / (y_train_orig + 1.0)
    w_train = w_train / w_train.mean()

    print(f"  Train: {len(y_train)}  Test: {len(y_test)}  Features: {X.shape[1]}")
    print(f"  Target: mean={y_train_orig.mean():.1f}h  std={y_train_orig.std():.1f}h")

    # LightGBM
    print("\n── LightGBM ──")
    dtrain = lgb.Dataset(X_train, label=y_train, weight=w_train)
    dtest = lgb.Dataset(X_test, label=y_test, reference=dtrain)

    params = {
        "objective": "regression", "metric": "rmse", "boosting_type": "gbdt",
        "num_leaves": 127, "learning_rate": 0.05,
        "feature_fraction": 0.75, "bagging_fraction": 0.8, "bagging_freq": 5,
        "min_data_in_leaf": 30,
        "lambda_l1": 0.1, "lambda_l2": 1.0,
        "verbose": -1, "num_threads": 4, "seed": 42,
    }

    model = lgb.train(params, dtrain, num_boost_round=1000,
                      valid_sets=[dtest],
                      callbacks=[lgb.early_stopping(50), lgb.log_evaluation(0)])

    yp_train = model.predict(X_train)
    yp_test = model.predict(X_test)
    if LOG_TARGET:
        yp_train = np.expm1(yp_train)
        yp_test = np.expm1(yp_test)

    print("\n── Train ──")
    evaluate(y_train_orig, yp_train, X_train)

    print("\n── Test ──")
    evaluate(y_test_orig, yp_test, X_test)

    # Feature importance
    print("\n── Top 15 features ──")
    importances = model.feature_importance(importance_type="gain")
    for i in np.argsort(importances)[::-1][:15]:
        print(f"  {feature_names[i]:30s}: {importances[i] / importances.sum() * 100:.1f}%")

    # Error by horizon
    print("\n── Error by horizon ──")
    for lo, hi, name in [(0, 1, "0-1h"), (1, 6, "1-6h"), (6, 24, "6-24h"), (24, 72, "1-3d"), (72, 200, "3-8d")]:
        mask = (y_test_orig >= lo) & (y_test_orig < hi)
        if mask.sum() > 0:
            err = np.abs(y_test_orig[mask] - yp_test[mask])
            print(f"  {name:6s}: MAE={err.mean():.1f}h  n={mask.sum()}")

    print("\n✓ Done.")


if __name__ == "__main__":
    main()
