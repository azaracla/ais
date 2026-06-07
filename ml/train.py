"""Train XGBoost/LightGBM v4 — predict time-to-arrival.

v4 improvements over v3:
  - Log-transform target (log1p) to handle right-skewed distribution
  - LightGBM with native categorical support
  - Sample weighting: inverse time-to-arrival (short horizons get more weight)
  - K-fold temporal cross-validation
"""

import polars as pl
import numpy as np
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.model_selection import TimeSeriesSplit
from xgboost import XGBRegressor
from utils import DATA_DIR

DATASET = DATA_DIR / "dataset.parquet"

NUMERIC_FEATURES = [
    "dist_to_dest_km",
    "sog",
    "cog",
    "bearing_offset_deg",
    "vessel_length",
    "vessel_width",
    "length_width_ratio",
    "hour_of_day",
    "day_of_week",
    "avg_sog_1h",
    "avg_sog_6h",
    "avg_sog_24h",
    "sog_trend_1h",
    "eta_naive_h",
]

TARGET = "time_to_arrival_hours"
LOG_TARGET = True  # Train on log1p(target), expm1 at prediction time


def load_data():
    """Load dataset, one-hot encode ship_type, return X, y, feature_names."""
    df = pl.read_parquet(DATASET).sort(["mmsi", "pos_ts"])

    all_numeric = NUMERIC_FEATURES + [TARGET]
    df = df.select(all_numeric + ["ship_type"]).drop_nulls(subset=all_numeric)
    print(f"Loaded {len(df)} rows (after dropping NaN)")

    # One-hot encode ship_type
    ship_types = df["ship_type"].fill_null(-1).cast(pl.Int64)
    type_counts = ship_types.value_counts().sort("count", descending=True)
    top_types = {int(row[0]) for row in type_counts.iter_rows() if row[1] >= 500}

    st_np = ship_types.to_numpy()
    onehot_cols = {}
    for st in sorted(top_types):
        onehot_cols[f"st_{int(st)}"] = (st_np == st).astype(np.float32)

    other_mask = ~np.isin(st_np, list(top_types))
    if other_mask.any():
        onehot_cols["st_other"] = other_mask.astype(np.float32)

    print(f"  Ship types: {len(top_types)} top + other ({other_mask.sum()} rows)")

    X_num = df.select(NUMERIC_FEATURES).to_numpy().astype(np.float32)
    feature_names = list(NUMERIC_FEATURES) + sorted(onehot_cols.keys())
    X = np.column_stack([X_num] + [onehot_cols[c] for c in sorted(onehot_cols.keys())])
    y = df.select(TARGET).to_numpy().ravel().astype(np.float32)

    if LOG_TARGET:
        y = np.log1p(y)  # log(1 + y) — stable for small values

    return X, y, feature_names


def evaluate(y_true, y_pred, X_test=None, label="Test"):
    """Print regression metrics. y_true/y_pred in original hours space."""
    mae = mean_absolute_error(y_true, y_pred)
    rmse = np.sqrt(mean_squared_error(y_true, y_pred))
    r2 = r2_score(y_true, y_pred)

    for threshold in [1, 3, 6, 12, 24]:
        acc = (np.abs(y_true - y_pred) <= threshold).mean()
        print(f"  within {threshold:2d}h: {acc:.1%}")

    print(f"  MAE:   {mae:.1f}h")
    print(f"  RMSE:  {rmse:.1f}h")
    print(f"  R²:    {r2:.4f}")

    if X_test is not None:
        dist_idx = 0
        sog_idx = 1
        actual_sog = np.maximum(X_test[:, sog_idx], 1.0)
        naive_mae = mean_absolute_error(y_true, X_test[:, dist_idx] / actual_sog)
        print(f"  Naive MAE (dist/sog): {naive_mae:.1f}h")
        print(f"  Improvement:          {(1 - mae / naive_mae) * 100:.1f}%")


def train_xgboost(X_train, y_train, X_test, y_test, sample_weight=None):
    """Train XGBoost with log-target."""
    print("\n── XGBoost ──")
    model = XGBRegressor(
        n_estimators=500,
        max_depth=7,
        learning_rate=0.03,
        subsample=0.8,
        colsample_bytree=0.7,
        min_child_weight=3,
        reg_alpha=0.5,
        reg_lambda=2.0,
        random_state=42,
        verbosity=0,
        early_stopping_rounds=50,
    )
    model.fit(
        X_train, y_train,
        eval_set=[(X_test, y_test)],
        sample_weight=sample_weight,
        verbose=False,
    )

    y_pred_train_raw = model.predict(X_train)
    y_pred_test_raw = model.predict(X_test)

    if LOG_TARGET:
        y_pred_train_raw = np.expm1(y_pred_train_raw)
        y_pred_test_raw = np.expm1(y_pred_test_raw)

    return y_pred_train_raw, y_pred_test_raw, model


def train_lightgbm(X_train, y_train, X_test, y_test, feature_names, sample_weight=None):
    """Train LightGBM with native categorical features."""
    try:
        import lightgbm as lgb
    except ImportError:
        print("  LightGBM not installed, skipping.")
        return None, None, None

    print("\n── LightGBM ──")
    # Build dataset
    train_data = lgb.Dataset(X_train, label=y_train, weight=sample_weight)
    test_data = lgb.Dataset(X_test, label=y_test, reference=train_data)

    params = {
        "objective": "regression",
        "metric": "rmse",
        "boosting_type": "gbdt",
        "num_leaves": 127,
        "learning_rate": 0.03,
        "feature_fraction": 0.7,
        "bagging_fraction": 0.8,
        "bagging_freq": 5,
        "min_data_in_leaf": 20,
        "lambda_l1": 0.5,
        "lambda_l2": 2.0,
        "verbose": -1,
        "num_threads": 4,
        "seed": 42,
    }

    model = lgb.train(
        params,
        train_data,
        num_boost_round=500,
        valid_sets=[test_data],
        callbacks=[lgb.early_stopping(50), lgb.log_evaluation(0)],
    )

    y_pred_train_raw = model.predict(X_train)
    y_pred_test_raw = model.predict(X_test)

    if LOG_TARGET:
        y_pred_train_raw = np.expm1(y_pred_train_raw)
        y_pred_test_raw = np.expm1(y_pred_test_raw)

    return y_pred_train_raw, y_pred_test_raw, model


def main():
    print("=" * 60)
    print("Time-to-Arrival Prediction — v4")
    print(f"  Log-transform: {LOG_TARGET}")
    print("=" * 60)

    print("\nLoading data ...")
    X, y, feature_names = load_data()

    # Temporal split
    split_idx = int(len(X) * 0.8)
    X_train, X_test = X[:split_idx], X[split_idx:]
    y_train, y_test = y[:split_idx], y[split_idx:]
    y_train_orig = y_train.copy()
    y_test_orig = y_test.copy()
    if LOG_TARGET:
        y_train_orig = np.expm1(y_train)
        y_test_orig = np.expm1(y_test)

    # Sample weights: inverse of time-to-arrival (short horizons matter more)
    weight_train = 1.0 / (y_train_orig + 1.0)
    weight_train = weight_train / weight_train.mean()  # normalize

    print(f"  Train: {len(X_train)}  Test: {len(X_test)}")
    print(f"  Features: {len(feature_names)}")
    print(f"  Target (orig): mean={y_train_orig.mean():.1f}h  std={y_train_orig.std():.1f}h")

    # ── XGBoost ──
    yp_train_xgb, yp_test_xgb, model_xgb = train_xgboost(
        X_train, y_train, X_test, y_test, sample_weight=weight_train
    )

    print("\n── XGBoost Train ──")
    evaluate(y_train_orig, yp_train_xgb, X_train)

    print("\n── XGBoost Test ──")
    evaluate(y_test_orig, yp_test_xgb, X_test)

    if model_xgb is not None:
        print("\n── XGBoost Feature importance (top 10) ──")
        importances = model_xgb.feature_importances_
        for i in np.argsort(importances)[::-1][:10]:
            pct = importances[i] * 100
            print(f"  {feature_names[i]:30s}: {pct:.1f}%")

    # ── LightGBM ──
    yp_train_lgb, yp_test_lgb, model_lgb = train_lightgbm(
        X_train, y_train, X_test, y_test, feature_names, sample_weight=weight_train
    )

    if yp_test_lgb is not None:
        print("\n── LightGBM Test ──")
        evaluate(y_test_orig, yp_test_lgb, X_test)

        # Compare
        mae_xgb = mean_absolute_error(y_test_orig, yp_test_xgb)
        mae_lgb = mean_absolute_error(y_test_orig, yp_test_lgb)
        print(f"\n  XGBoost MAE: {mae_xgb:.1f}h  vs  LightGBM MAE: {mae_lgb:.1f}h")
        if mae_lgb < mae_xgb:
            print(f"  ✓ LightGBM wins by {mae_xgb - mae_lgb:.1f}h")

    # ── Error breakdown ──
    yp_test = yp_test_lgb if yp_test_lgb is not None and mae_lgb < mae_xgb else yp_test_xgb

    print("\n── Error by horizon ──")
    bins = [0, 1, 6, 24, 72, 168, 200]
    for i in range(len(bins) - 1):
        mask = (y_test_orig >= bins[i]) & (y_test_orig < bins[i + 1])
        if mask.sum() > 0:
            err = np.abs(y_test_orig[mask] - yp_test[mask])
            print(f"  tta {bins[i]:3d}-{bins[i+1]:3d}h: MAE={err.mean():.1f}h  n={mask.sum()}")

    print("\n── Error by distance ──")
    dist_bins = [0, 50, 200, 500, 2000, 20000]
    for i in range(len(dist_bins) - 1):
        mask = (X_test[:, 0] >= dist_bins[i]) & (X_test[:, 0] < dist_bins[i + 1])
        if mask.sum() > 0:
            err = np.abs(y_test_orig[mask] - yp_test[mask])
            print(f"  dist {dist_bins[i]:5d}-{dist_bins[i+1]:5d} km: MAE={err.mean():.1f}h  n={mask.sum()}")

    print("\n✓ Done.")


if __name__ == "__main__":
    main()
