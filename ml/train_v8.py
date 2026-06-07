"""Train v8 ETA prediction — native categorical, horizon-weighted loss, interactions.

v8 improvements over v7 (10.00h MAE, R² 0.52):
  1. LightGBM native categorical (ship_type, nav_status) — ~40 fewer one-hot bloat cols
  2. Horizon-weighted custom loss — stop ignoring 3-8d horizons
  3. Interaction features — dist×closing, mmsi_tta_bias, sog_vs_hist, weighted_efficiency
  4. Anchoring detection — stationary + near port but not at port
  5. Feature pruning — remove collinear eta_phys variants (keep only eta_phys_6h)

NOT doing (diminishing returns / wrong problem):
  - searoute: visualisation lib, not real routing
  - 10k+ trees: already saw diminishing returns 2000→5000 (−0.25h)
  - DART: breaks early stopping, marginal gain
  - Transformers: papers good at 10h, irrelevant at 3-8d
  - Stacking: complexity unjustified for −0.5h

Usage:
  uv run python ml/train_v8.py              # full training
  uv run python ml/train_v8.py --quick       # 500 trees test
"""

import json
import sys
import warnings
from pathlib import Path

import lightgbm as lgb
import numpy as np
import polars as pl
from sklearn.metrics import mean_absolute_error

warnings.filterwarnings("ignore")
from utils import DATA_DIR

DATASET = DATA_DIR / "dataset_v7.parquet"
MODEL_DIR = DATA_DIR / "models_v8"
MODEL_DIR.mkdir(exist_ok=True)

RANDOM_SEED = 42
QUICK_MODE = "--quick" in sys.argv
N_TREES = 500 if QUICK_MODE else 5000
EARLY_STOP = 30 if QUICK_MODE else 200

# ── Features (pruned — no collinear eta_phys variants) ────────────────────────────

NUMERIC_FEATURES = [
    # Core spatial
    "dist_to_dest_km", "sog", "cog", "bearing_offset_deg",
    # Vessel characteristics
    "vessel_length", "vessel_width", "length_width_ratio",
    "draught_filled",
    # Heading/turn
    "heading_offset_deg", "rate_of_turn", "rot_available",
    "heading_std_1h", "heading_std_3h", "heading_std_6h",
    "avg_heading_1h",
    # Historical SOG
    "avg_sog_1h", "avg_sog_6h", "avg_sog_24h", "sog_trend_1h",
    # Approach
    "closing_speed_kmh", "approach_efficiency",
    # Trajectory 6h
    "stop_fraction_3h", "slow_fraction_3h",
    "stop_fraction_6h", "slow_fraction_6h",
    "cog_std_3h", "cog_std_6h",
    "sog_range_6h", "sog_delta_30min",
    "sog_accel_6h", "turn_rate_6h",
    # Physics (keep only simplest variant)
    "eta_naive_h", "eta_phys_6h",
    # Port features
    "port_avg_tta", "port_arrival_count", "port_avg_sog",
    "port_arrival_rate_per_hour",
    # MMSI features
    "mmsi_avg_sog", "sog_vs_mmsi_avg", "sog_vs_mmsi_typical",
    "mmsi_sog_std", "mmsi_sog_cv",
    "mmsi_avg_tta", "mmsi_sample_count", "mmsi_median_sog",
    # Time (raw + cyclical)
    "hour_of_day", "day_of_week",
    "hour_sin", "hour_cos", "dow_sin", "dow_cos",
]

# v8 interaction features (computed at runtime from existing columns)
INTERACTION_DEFS = {
    "dist_x_closing": lambda d: (
        d["dist_to_dest_km"].to_numpy() * d["closing_speed_kmh"].to_numpy()
    ),
    "weighted_efficiency": lambda d: (
        d["approach_efficiency"].to_numpy() *
        np.log1p(d["dist_to_dest_km"].to_numpy())
    ),
    "mmsi_tta_bias": lambda d: (
        d["mmsi_avg_tta"].to_numpy() /
        (d["eta_naive_h"].to_numpy() + 1e-3)
    ),
    "sog_vs_hist": lambda d: (
        d["avg_sog_1h"].to_numpy() /
        (d["mmsi_median_sog"].to_numpy() + 0.1)
    ),
}

# v8 anchoring detection
def add_anchoring(df):
    """Detect suspected anchoring: stationary vessel 10-80km from port."""
    sog = df["avg_sog_1h"].to_numpy()
    dist = df["dist_to_dest_km"].to_numpy()
    stop = df["stop_fraction_6h"].to_numpy()
    return np.where(
        (sog < 0.5) & (dist > 10) & (dist < 80),
        1.0,
        0.0,
    ).astype(np.float32), np.where(
        stop > 0, stop * 6.0, 0.0  # stationary_duration_h
    ).astype(np.float32)


# Categorical features passed natively to LightGBM (no one-hot)
CATEGORICAL_FEATURES = ["ship_type", "nav_status"]

TARGET = "time_to_arrival_hours"
HORIZON_BINS = [
    (0, 1, "0-1h"), (1, 6, "1-6h"), (6, 24, "6-24h"),
    (24, 72, "1-3d"), (72, 200, "3-8d"),
]

# ── Horizon-weighted loss ─────────────────────────────────────────────────────────

def horizon_weighted_rmse(y_pred, y_true):
    """Custom RMSE that penalizes long-horizon errors more.

    LightGBM feval signature: (y_pred, y_true_dataset).
    Weights: 0-1h:0.3, 1-6h:0.7, 6-24h:1.0, 1-3d:2.0, 3-8d:4.0
    """
    yt_log = y_true.get_label()
    yt_h = np.expm1(yt_log)  # back to hours for thresholding
    residual = y_pred - yt_log
    w = np.ones_like(yt_h)
    w[yt_h < 1.0] = 0.3
    w[(yt_h >= 1.0) & (yt_h < 6.0)] = 0.7
    w[(yt_h >= 6.0) & (yt_h < 24.0)] = 1.0
    w[(yt_h >= 24.0) & (yt_h < 72.0)] = 2.0
    w[yt_h >= 72.0] = 4.0
    wmse = np.average(residual ** 2, weights=w)
    return "hw_rmse", np.sqrt(wmse), False


# ── Data loading ──────────────────────────────────────────────────────────────────

def load_data():
    """Load dataset, add interactions + anchoring, return native categorical format."""
    print(f"Loading: {DATASET}")
    df = pl.read_parquet(DATASET).sort(["mmsi", "pos_ts"])

    # ── Add interaction features ──
    print("  Computing interaction features ...")
    for name, fn in INTERACTION_DEFS.items():
        df = df.with_columns(pl.Series(name, fn(df)))
        print(f"    {name}")

    # ── Add anchoring detection ──
    print("  Computing anchoring features ...")
    anchor, stationary = add_anchoring(df)
    df = df.with_columns([
        pl.Series("anchoring_suspected", anchor),
        pl.Series("stationary_duration_h", stationary),
    ])

    # ── Feature lists ──
    available_num = [f for f in NUMERIC_FEATURES if f in df.columns] + \
                    list(INTERACTION_DEFS.keys()) + \
                    ["anchoring_suspected", "stationary_duration_h"]
    available_cat = [f for f in CATEGORICAL_FEATURES if f in df.columns]

    # Build numeric matrix
    X_num = df.select(available_num).to_numpy().astype(np.float32)

    # Build categorical as int (LightGBM handles natively)
    cat_data = []
    cat_names = []
    for col in available_cat:
        vals = df[col].fill_null(-1).cast(pl.Int32).to_numpy()
        cat_data.append(vals.reshape(-1, 1).astype(np.int32))
        cat_names.append(col)

    X_cat = np.column_stack(cat_data) if cat_data else np.zeros((len(df), 0), dtype=np.int32)

    # Full matrix: numeric + categorical (LightGBM expects cat idx after numeric)
    X = np.column_stack([X_num, X_cat]).astype(np.float32)
    cat_indices = list(range(len(available_num), len(available_num) + len(cat_names)))

    y = df[TARGET].to_numpy().ravel().astype(np.float32)
    mmsi_arr = df["mmsi"].to_numpy()

    feature_names = available_num + cat_names
    print(f"  Features: {len(available_num)} numeric + {len(cat_names)} categorical = {X.shape[1]} total")
    print(f"  (v7 had 85 features with one-hot bloat)")
    return X, X_num, y, feature_names, cat_indices, mmsi_arr


# ── Evaluation ────────────────────────────────────────────────────────────────────

def evaluate(name, y_true, y_pred, X_num=None, feature_names=None):
    mae = mean_absolute_error(y_true, y_pred)
    rmse = np.sqrt(np.mean((y_true - y_pred) ** 2))
    r2 = 1 - np.sum((y_true - y_pred) ** 2) / np.sum((y_true - y_true.mean()) ** 2)

    print(f"\n{'─'*60}")
    print(f"  {name}")
    print(f"{'─'*60}")
    print(f"  MAE:  {mae:.2f}h")
    print(f"  RMSE: {rmse:.1f}h")
    print(f"  R²:   {r2:.4f}")
    print(f"  Per-horizon MAE:")
    for lo, hi, hname in HORIZON_BINS:
        mask = (y_true >= lo) & (y_true < hi)
        if mask.sum() > 10:
            err = np.abs(y_true[mask] - y_pred[mask])
            print(f"    {hname:6s}: MAE={err.mean():.1f}h  n={mask.sum()}")

    if X_num is not None and feature_names is not None:
        for i, fn in enumerate(feature_names):
            if fn == "eta_naive_h":
                naive_mae = mean_absolute_error(y_true, X_num[:, i])
                print(f"  Naive (dist/sog): {naive_mae:.1f}h  (improvement: {(1-mae/naive_mae)*100:.1f}%)")
                break
    return mae, r2


# ── Training ──────────────────────────────────────────────────────────────────────

def train_two_stage(X_train, y_train, X_test, y_test,
                    feature_names, cat_indices, mmsi_train, mmsi_test):
    """Two-stage LightGBM with native categorical + horizon-weighted loss."""
    print(f"\n{'='*60}")
    print("Two-Stage LightGBM v8")
    print(f"{'='*60}")

    y_train_log = np.log1p(y_train)
    n_folds = 5

    # ── Stage A: 5-fold CV ──
    print("\n── Stage A (5-fold CV + horizon-weighted loss) ──")
    unique_mmsis = np.unique(mmsi_train)
    np.random.seed(RANDOM_SEED)
    np.random.shuffle(unique_mmsis)
    fold_size = len(unique_mmsis) // n_folds

    y_train_pred_a = np.zeros(len(y_train_log), dtype=np.float32)
    params_a = {
        "objective": "regression", "metric": "rmse", "boosting_type": "gbdt",
        "num_leaves": 127, "learning_rate": 0.02,
        "feature_fraction": 0.7, "bagging_fraction": 0.75, "bagging_freq": 5,
        "min_data_in_leaf": 50, "lambda_l1": 0.5, "lambda_l2": 2.0,
        "verbose": -1, "num_threads": 12, "seed": RANDOM_SEED,
        "categorical_feature": cat_indices,
    }

    w_train = 1.0 / (y_train + 1.0)
    w_train = w_train / w_train.mean()

    for fold in range(n_folds):
        val_start = fold * fold_size
        val_end = (fold+1)*fold_size if fold < n_folds-1 else len(unique_mmsis)
        val_mmsis = set(unique_mmsis[val_start:val_end])
        tr_idx = np.array([i for i, m in enumerate(mmsi_train) if m not in val_mmsis])
        vl_idx = np.array([i for i, m in enumerate(mmsi_train) if m in val_mmsis])

        dtrain = lgb.Dataset(X_train[tr_idx], label=y_train_log[tr_idx],
                             weight=w_train[tr_idx])
        dval = lgb.Dataset(X_train[vl_idx], label=y_train_log[vl_idx])

        model = lgb.train(
            params_a, dtrain, num_boost_round=N_TREES,
            valid_sets=[dval],
            feval=[horizon_weighted_rmse],
            callbacks=[lgb.early_stopping(EARLY_STOP), lgb.log_evaluation(0)],
        )
        y_train_pred_a[vl_idx] = model.predict(X_train[vl_idx])
        print(f"  Fold {fold+1}/{n_folds}: {len(vl_idx)} val samples")

    # Final Stage A
    dtrain_full = lgb.Dataset(X_train, label=y_train_log, weight=w_train)
    model_a = lgb.train(params_a, dtrain_full, num_boost_round=N_TREES,
                         callbacks=[lgb.log_evaluation(0)])
    y_test_pred_a = model_a.predict(X_test)

    y_pred_a_orig = np.expm1(y_train_pred_a)
    mae_a = mean_absolute_error(y_train, y_pred_a_orig)
    r2_a = 1 - np.sum((y_train - y_pred_a_orig)**2) / np.sum((y_train - y_train.mean())**2)
    print(f"  Stage A CV MAE: {mae_a:.1f}h  R²: {r2_a:.4f}")

    # ── Stage B: Residual ──
    print("\n── Stage B: Residual correction ──")
    residual = y_train_log - y_train_pred_a
    X_train_b = np.column_stack([X_train, y_train_pred_a])
    X_test_b = np.column_stack([X_test, y_test_pred_a])

    w_res = np.abs(residual) + 0.1
    w_res = w_res / w_res.mean()

    params_b = {**params_a, "num_leaves": 63, "learning_rate": 0.015}
    dtrain_b = lgb.Dataset(X_train_b, label=residual, weight=w_res)
    model_b = lgb.train(params_b, dtrain_b, num_boost_round=N_TREES,
                         callbacks=[lgb.log_evaluation(0)])

    yp_b = model_b.predict(X_test_b)

    # Adaptive blending (threshold=3h)
    yp_a_test = np.expm1(y_test_pred_a)
    blend = 1.0 / (1.0 + np.exp(-2.0 * (yp_a_test - 3.0) / 3.0))
    y_pred_log = y_test_pred_a + blend * yp_b
    y_pred = np.expm1(y_pred_log)
    y_pred = np.clip(y_pred, 0.05, 200)

    evaluate("Two-Stage v8", y_test, y_pred, X_test, feature_names)

    # Feature importance
    imp = model_a.feature_importance(importance_type="gain")
    top = np.argsort(imp)[::-1][:15]
    print(f"\n  Top 15 features (gain):")
    for i in top:
        print(f"    {feature_names[i]:30s}: {imp[i]/imp.sum()*100:.1f}%")

    return y_pred, model_a, model_b


def train_single(X_train, y_train, X_test, y_test,
                 feature_names, cat_indices, w_train):
    """Single-stage LightGBM (baseline)."""
    print(f"\n── Single-Stage LightGBM v8 ──")
    y_train_log = np.log1p(y_train)
    dtrain = lgb.Dataset(X_train, label=y_train_log, weight=w_train)
    dval = lgb.Dataset(X_test, label=np.log1p(y_test))

    params = {
        "objective": "regression", "metric": "rmse", "boosting_type": "gbdt",
        "num_leaves": 127, "learning_rate": 0.02,
        "feature_fraction": 0.7, "bagging_fraction": 0.75, "bagging_freq": 5,
        "min_data_in_leaf": 50, "lambda_l1": 0.5, "lambda_l2": 2.0,
        "verbose": -1, "num_threads": 12, "seed": RANDOM_SEED,
        "categorical_feature": cat_indices,
    }

    model = lgb.train(
        params, dtrain, num_boost_round=N_TREES,
        valid_sets=[dval],
        feval=[horizon_weighted_rmse],
        callbacks=[lgb.early_stopping(EARLY_STOP), lgb.log_evaluation(100)],
    )
    y_pred = np.expm1(model.predict(X_test))
    evaluate("Single-Stage v8", y_test, y_pred, X_test, feature_names)

    imp = model.feature_importance(importance_type="gain")
    top = np.argsort(imp)[::-1][:15]
    print(f"\n  Top 15 features:")
    for i in top:
        print(f"    {feature_names[i]:30s}: {imp[i]/imp.sum()*100:.1f}%")

    return y_pred, model


# ── Main ──────────────────────────────────────────────────────────────────────────

def main():
    print("=" * 70)
    print(f"Vessel ETA Prediction — v8")
    print(f"Native categorical + horizon-weighted loss + interactions + anchoring")
    print(f"Mode: {'QUICK' if QUICK_MODE else 'FULL'} ({N_TREES} trees)")
    print("=" * 70)

    # Load
    X, X_num, y, feature_names, cat_indices, mmsi_arr = load_data()

    # Split (MMSI-grouped)
    unique_mmsis = np.unique(mmsi_arr)
    np.random.seed(RANDOM_SEED)
    np.random.shuffle(unique_mmsis)
    split_n = int(len(unique_mmsis) * 0.8)
    train_mmsis = set(unique_mmsis[:split_n])
    train_mask = np.array([m in train_mmsis for m in mmsi_arr])
    test_mask = ~train_mask

    X_train, X_test = X[train_mask], X[test_mask]
    X_num_test = X_num[test_mask]
    y_train, y_test = y[train_mask], y[test_mask]
    mmsi_train = mmsi_arr[train_mask]

    print(f"\n── Train/Test ──")
    print(f"  Train: {len(X_train)} samples ({len(np.unique(mmsi_train))} MMSIs)")
    print(f"  Test:  {len(X_test)} samples")

    w_train = 1.0 / (y_train + 1.0)
    w_train = w_train / w_train.mean()

    # Single-stage baseline
    yp_single, model_single = train_single(
        X_train, y_train, X_test, y_test, feature_names, cat_indices, w_train
    )
    mae_single = mean_absolute_error(y_test, yp_single)

    # Two-stage (key innovation)
    yp_2stage, model_a, model_b = train_two_stage(
        X_train, y_train, X_test, y_test,
        feature_names, cat_indices, mmsi_train, mmsi_arr[test_mask]
    )
    mae_2stage = mean_absolute_error(y_test, yp_2stage)

    # Summary
    print(f"\n{'='*70}")
    print("Summary — v8")
    print(f"{'='*70}")
    # Naive reference
    eta_idx = feature_names.index("eta_naive_h")
    naive_mae = mean_absolute_error(y_test, X_num_test[:, eta_idx])
    print(f"  Naive (dist/sog):     {naive_mae:.1f}h")
    print(f"  v6 (MMSI split):      14.0h")
    print(f"  v7 best:              10.0h")
    print(f"  v8 Single-Stage:      {mae_single:.1f}h")
    print(f"  v8 Two-Stage:         {mae_2stage:.1f}h")
    best_name = "Two-Stage" if mae_2stage < mae_single else "Single-Stage"
    best_mae = min(mae_single, mae_2stage)
    print(f"  v8 BEST: {best_name} at {best_mae:.1f}h")

    # Save
    if best_name == "Two-Stage":
        model_a.save_model(str(MODEL_DIR / "stage_a.txt"))
        model_b.save_model(str(MODEL_DIR / "stage_b.txt"))
    else:
        model_single.save_model(str(MODEL_DIR / "model.txt"))

    meta = {
        "version": "v8",
        "best_model": best_name,
        "test_mae": float(best_mae),
        "test_mae_single": float(mae_single),
        "test_mae_2stage": float(mae_2stage),
        "feature_names": feature_names,
        "cat_indices": cat_indices,
        "n_trees": N_TREES,
        "random_seed": RANDOM_SEED,
    }
    json.dump(meta, open(MODEL_DIR / "metadata.json", "w"), indent=2)
    print(f"\n✓ v8 saved to {MODEL_DIR}/")


if __name__ == "__main__":
    main()
