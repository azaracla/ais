"""Train XGBoost v2 — predict time-to-arrival (hours remaining).

Target: time_to_arrival_hours = arrival_ts - current_position_ts.

No dependency on declared ETA → model works for ALL vessels with a destination.
Can predict ETA for vessels that never declare one (~68% of fleet).
"""

import polars as pl
import numpy as np
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from xgboost import XGBRegressor
from utils import DATA_DIR

DATASET = DATA_DIR / "dataset.parquet"

FEATURE_COLS = [
    "dist_to_dest_km",
    "sog",
    "cog",
    "bearing_offset_deg",
    "vessel_length",
    "vessel_width",
    "length_width_ratio",
    "ship_type",
    "hour_of_day",
    "day_of_week",
]

TARGET = "time_to_arrival_hours"


def load_data() -> tuple[np.ndarray, np.ndarray]:
    """Load dataset, return X, y."""
    df = pl.read_parquet(DATASET).sort(["mmsi", "pos_ts"])

    all_cols = FEATURE_COLS + [TARGET]
    df = df.select(all_cols).drop_nulls()
    print(f"Loaded {len(df)} rows (after dropping NaN)")

    X = df.select(FEATURE_COLS).to_numpy().astype(np.float32)
    y = df.select(TARGET).to_numpy().ravel().astype(np.float32)
    return X, y


def evaluate(y_true, y_pred, label="Test"):
    """Print regression metrics."""
    mae = mean_absolute_error(y_true, y_pred)
    rmse = np.sqrt(mean_squared_error(y_true, y_pred))
    r2 = r2_score(y_true, y_pred)

    # Within-N-hours accuracy
    for threshold in [1, 3, 6, 12, 24]:
        acc = (np.abs(y_true - y_pred) <= threshold).mean()
        print(f"  within {threshold:2d}h: {acc:.1%}")

    print(f"  MAE:   {mae:.1f}h")
    print(f"  RMSE:  {rmse:.1f}h")
    print(f"  R²:    {r2:.4f}")

    # Naive baseline: time = distance / avg_speed (10 kn)
    avg_speed = 10.0
    X_test = None  # set by caller
    naive_pred = X_test[:, 0] / avg_speed if X_test is not None else None
    if naive_pred is not None:
        naive_mae = mean_absolute_error(y_true, naive_pred)
        print(f"  Naive MAE (dist/10kn): {naive_mae:.1f}h")
        print(f"  Improvement vs naive:  {(1 - mae / naive_mae) * 100:.1f}%")


def main():
    print("=" * 60)
    print("Time-to-Arrival Prediction — XGBoost v2")
    print("=" * 60)

    print("\nLoading data ...")
    X, y = load_data()

    # Temporal split: sort by pos_ts, first 80% train
    split_idx = int(len(X) * 0.8)
    X_train, X_test = X[:split_idx], X[split_idx:]
    y_train, y_test = y[:split_idx], y[split_idx:]

    # For naive baseline
    evaluate.__globals__["X_test"] = X_test

    print(f"  Train: {len(X_train)}  Test: {len(X_test)}")
    print(f"  Target: mean={y.mean():.1f}h  std={y.std():.1f}h  range=[{y.min():.1f}, {y.max():.1f}]h")

    # ── Train ──
    print("\nTraining XGBoost ...")
    model = XGBRegressor(
        n_estimators=300,
        max_depth=6,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        min_child_weight=5,
        reg_alpha=0.1,
        reg_lambda=1.0,
        random_state=42,
        verbosity=1,
    )
    model.fit(
        X_train, y_train,
        eval_set=[(X_test, y_test)],
        verbose=False,
    )

    # ── Evaluate ──
    print("\n── Train ──")
    y_pred_train = model.predict(X_train)
    evaluate(y_train, y_pred_train, "Train")

    print("\n── Test ──")
    y_pred_test = model.predict(X_test)
    evaluate(y_test, y_pred_test, "Test")

    # ── Feature importance ──
    print("\n── Feature importance ──")
    importances = model.feature_importances_
    for i in np.argsort(importances)[::-1]:
        print(f"  {FEATURE_COLS[i]:25s}: {importances[i]:.4f}")

    # ── Error by time horizon ──
    print("\n── Error by true time-to-arrival ──")
    bins = [0, 1, 6, 24, 72, 168, 200]
    for i in range(len(bins) - 1):
        mask = (y_test >= bins[i]) & (y_test < bins[i + 1])
        if mask.sum() > 0:
            err = np.abs(y_test[mask] - y_pred_test[mask])
            print(f"  tta {bins[i]:3d}-{bins[i+1]:3d}h: "
                  f"MAE={err.mean():.1f}h  n={mask.sum()}")

    # ── Error by distance ──
    print("\n── Error by distance to destination ──")
    dist_bins = [0, 50, 200, 500, 2000, 20000]
    for i in range(len(dist_bins) - 1):
        mask = (X_test[:, 0] >= dist_bins[i]) & (X_test[:, 0] < dist_bins[i + 1])
        if mask.sum() > 0:
            err = np.abs(y_test[mask] - y_pred_test[mask])
            print(f"  dist {dist_bins[i]:5d}-{dist_bins[i+1]:5d} km: "
                  f"MAE={err.mean():.1f}h  n={mask.sum()}")

    # ── Scatter stats ──
    print("\n── Prediction distribution ──")
    errors = y_test - y_pred_test
    print(f"  Error: mean={errors.mean():.1f}h  std={errors.std():.1f}h")
    print(f"  P10={np.percentile(errors, 10):.1f}h  P50={np.percentile(errors, 50):.1f}h  P90={np.percentile(errors, 90):.1f}h")
    print(f"  Under-predict (>10h early):  {(errors > 10).mean():.1%}")
    print(f"  Over-predict  (>10h late):   {(errors < -10).mean():.1%}")
    print(f"  Within ±10h:                 {((errors >= -10) & (errors <= 10)).mean():.1%}")


if __name__ == "__main__":
    main()
